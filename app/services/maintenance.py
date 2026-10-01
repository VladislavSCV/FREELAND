"""Housekeeping: remove test data from analytics and clear stale review queues."""
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import (
    Activity,
    Application,
    Client,
    FollowUp,
    Lead,
    LeadAnalysis,
    Payment,
    Project,
    Proposal,
)
from app.services import audit

# Leads created by scripts/smoke.py and `freelance seed --demo`.
DEMO_SOURCES = ("smoke", "seed")


def cleanup_demo(db: Session, sources: tuple[str, ...] = DEMO_SOURCES) -> dict[str, int]:
    """Delete demo/smoke leads with everything attached to them."""
    lead_ids = list(db.scalars(select(Lead.id).where(Lead.source.in_(sources))))
    if not lead_ids:
        return {"leads": 0}
    projects = db.scalars(select(Project).where(Project.lead_id.in_(lead_ids))).all()
    project_ids = [p.id for p in projects]
    client_ids = [p.client_id for p in projects if p.client_id]
    proposal_ids = list(db.scalars(select(Proposal.id).where(Proposal.lead_id.in_(lead_ids))))
    counts = {"leads": len(lead_ids), "projects": len(project_ids), "proposals": len(proposal_ids)}
    for entity, ids in (("lead", lead_ids), ("project", project_ids), ("proposal", proposal_ids)):
        if ids:
            db.execute(delete(Activity).where(Activity.entity_type == entity, Activity.entity_id.in_(ids)))
    if project_ids:
        db.execute(delete(Payment).where(Payment.project_id.in_(project_ids)))
    db.execute(delete(FollowUp).where(FollowUp.lead_id.in_(lead_ids)))
    db.execute(delete(Application).where(Application.lead_id.in_(lead_ids)))
    db.execute(delete(Project).where(Project.lead_id.in_(lead_ids)))
    if client_ids:
        db.execute(delete(Client).where(Client.id.in_(client_ids)))
    db.execute(delete(Proposal).where(Proposal.lead_id.in_(lead_ids)))
    db.execute(delete(LeadAnalysis).where(LeadAnalysis.lead_id.in_(lead_ids)))
    db.execute(delete(Lead).where(Lead.id.in_(lead_ids)))
    db.commit()
    return counts


def reevaluate_archived(db: Session, accepts) -> int:
    """Send archived leads that pass `accepts(lead)` back to the autopilot (e.g. after profile/scoring changes).
    Their analysis, application and unsent proposals are dropped; the worker evaluates them again."""
    rows = db.execute(select(Application, Lead).join(Lead, Lead.id == Application.lead_id).where(Application.status == "ARCHIVED", Lead.status.in_(["ARCHIVED", "REJECTED"]))).all()
    count = 0
    for application, lead in rows:
        if not accepts(lead):
            continue
        db.execute(delete(Application).where(Application.id == application.id))
        db.execute(delete(LeadAnalysis).where(LeadAnalysis.lead_id == lead.id))
        db.execute(delete(Proposal).where(Proposal.lead_id == lead.id, Proposal.status.in_(["DRAFT", "REJECTED"])))
        lead.status = "NEW"
        lead.analysis_attempts = 0
        audit(db, "lead", lead.id, "requeued_for_evaluation")
        count += 1
    db.commit()
    return count


def archive_pending(db: Session) -> int:
    """Move every application waiting for review to the archive (lead and draft are rejected)."""
    applications = db.scalars(select(Application).where(Application.status == "PENDING_REVIEW")).all()
    for application in applications:
        application.status = "ARCHIVED"
        lead = db.get(Lead, application.lead_id)
        if lead and lead.status == "PROPOSAL_READY":
            lead.status = "REJECTED"
        if application.proposal_id and (proposal := db.get(Proposal, application.proposal_id)) and proposal.status == "DRAFT":
            proposal.status = "REJECTED"
        audit(db, "application", application.id, "archived_in_bulk")
    db.commit()
    return len(applications)
