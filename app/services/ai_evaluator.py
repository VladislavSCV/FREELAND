"""Autopilot evaluation: LLM analysis -> 1-10 score -> threshold routing.

Score >= AUTOPILOT_THRESHOLD with autopilot on: the proposal is approved automatically and a
ready-to-send card goes to Telegram (one tap to send it on the platform, one tap to mark sent).
REVIEW_SCORE..AUTOPILOT_THRESHOLD-1 (or any score >= REVIEW_SCORE with autopilot off): a review card
with Send / Edit / Skip. Below REVIEW_SCORE: the lead is archived without a proposal.
"""
import logging

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Application, AppSetting, FollowUp, Lead, Proposal, now
from app.services import analyze, approve, audit, generate_proposal, mark_sent, score_10, transition

log = logging.getLogger(__name__)
AUTOPILOT_KEY = "autopilot"


def autopilot_enabled(db: Session) -> bool:
    row = db.get(AppSetting, AUTOPILOT_KEY)
    return settings().autopilot_enabled if row is None else row.value == "on"


def set_autopilot(db: Session, enabled: bool):
    row = db.get(AppSetting, AUTOPILOT_KEY) or AppSetting(key=AUTOPILOT_KEY, value="")
    row.value = "on" if enabled else "off"
    db.add(row)
    audit(db, "settings", 0, f"autopilot_{row.value}")
    db.commit()


async def evaluate(db: Session, lead: Lead) -> Application:
    """Idempotent: resumes an application whose proposal generation failed earlier."""
    cfg = settings()
    application = db.scalar(select(Application).where(Application.lead_id == lead.id))
    if application and application.status != "PROPOSAL_PENDING":
        return application
    if application is None:
        analysis = await analyze(db, lead)
        score = score_10(analysis.overall_score)
        reason = analysis.match_reason or (analysis.reasoning[0] if analysis.reasoning else "")
        application = Application(lead_id=lead.id, score=score, match_reason=reason, status="PROPOSAL_PENDING", decision="auto")
        db.add(application)
        if score < cfg.review_score:
            application.status = "ARCHIVED"
            transition(lead, "ARCHIVED")
            db.flush()
            audit(db, "application", application.id, "archived", {"score": score})
            db.commit()
            return application
        db.commit()
    proposal = await generate_proposal(db, lead)
    application.proposal_id = proposal.id
    application.proposal_draft = proposal.body
    if application.score >= cfg.auto_apply_score and autopilot_enabled(db):
        approve(db, lead)
        application.status = "APPROVED"
    else:
        application.status = "PENDING_REVIEW"
        application.decision = "manual"
    audit(db, "application", application.id, application.status.lower(), {"score": application.score})
    db.commit()
    return application


def _lead(db: Session, application: Application) -> Lead:
    lead = db.get(Lead, application.lead_id)
    if lead is None:
        raise HTTPException(404, "Lead not found")
    return lead


def approve_application(db: Session, application: Application) -> Application:
    if application.status != "PENDING_REVIEW":
        raise HTTPException(409, f"Application is {application.status}")
    approve(db, _lead(db, application))
    application.status = "APPROVED"
    application.decision = "manual"
    db.commit()
    return application


def mark_application_sent(db: Session, application: Application) -> Application:
    if application.status != "APPROVED":
        raise HTTPException(409, f"Application is {application.status}")
    mark_sent(db, _lead(db, application))
    application.status = "SENT"
    application.sent_at = now()
    db.commit()
    return application


def skip_application(db: Session, application: Application) -> Application:
    if application.status not in {"PENDING_REVIEW", "APPROVED"}:
        raise HTTPException(409, f"Application is {application.status}")
    lead = _lead(db, application)
    transition(lead, "REJECTED")
    if application.proposal_id and (proposal := db.get(Proposal, application.proposal_id)):
        proposal.status = "REJECTED"
    application.status = "SKIPPED"
    audit(db, "application", application.id, "skipped")
    db.commit()
    return application


def edit_application(db: Session, application: Application, body: str) -> Application:
    body = body.strip()
    if application.status != "PENDING_REVIEW":
        raise HTTPException(409, "Only proposals waiting for review can be edited")
    if not 20 <= len(body) <= 5000:
        raise HTTPException(422, "Proposal text must be 20-5000 characters")
    proposal = db.get(Proposal, application.proposal_id) if application.proposal_id else None
    if proposal is None or proposal.status != "DRAFT":
        raise HTTPException(409, "No draft proposal")
    proposal.body = body
    application.proposal_draft = body
    audit(db, "proposal", proposal.id, "edited", {"via": "telegram"})
    db.commit()
    return application


def daily_status(db: Session) -> dict:
    start = now().replace(hour=0, minute=0, second=0, microsecond=0)
    applications = db.scalars(select(Application).where(Application.created_at >= start)).all()
    by_status: dict[str, int] = {}
    for application in applications:
        by_status[application.status] = by_status.get(application.status, 0) + 1
    return {
        "autopilot": autopilot_enabled(db),
        "collected": len(db.scalars(select(Lead.id).where(Lead.created_at >= start)).all()),
        "evaluated": len(applications),
        "auto_approved": sum(a.decision == "auto" and a.status in {"APPROVED", "SENT"} for a in applications),
        "by_status": by_status,
        "sent": len(db.scalars(select(Proposal.id).where(Proposal.sent_at >= start)).all()),
        "waiting_review": len(db.scalars(select(Application.id).where(Application.status == "PENDING_REVIEW")).all()),
        "waiting_send": len(db.scalars(select(Application.id).where(Application.status == "APPROVED")).all()),
        "followups_due": len(db.scalars(select(FollowUp.id).where(FollowUp.status == "PENDING", FollowUp.due_at <= now())).all()),
    }
