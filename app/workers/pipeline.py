"""Autopilot pipeline: evaluate fresh leads and push decision cards to Telegram."""
import logging

from aiogram import Bot
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Application, Lead, Proposal, now
from app.services.ai_evaluator import evaluate
from app.telegram_ui import application_card, application_keyboard, broadcast

log = logging.getLogger(__name__)


async def evaluate_pending(db: Session) -> int:
    cfg = settings()
    resumable = select(Application.lead_id).where(Application.status == "PROPOSAL_PENDING")
    evaluated = select(Application.lead_id)
    leads = db.scalars(
        select(Lead)
        .where(or_(
            Lead.id.in_(resumable),
            Lead.status.in_(["NEW", "ANALYSIS_PENDING"]) & Lead.id.not_in(evaluated) & (Lead.analysis_attempts < cfg.max_analysis_attempts),
        ))
        .order_by(Lead.id)
        .limit(cfg.max_evaluations_per_tick)
    ).all()
    done = 0
    for lead in leads:
        try:
            await evaluate(db, lead)
            done += 1
        except Exception:
            db.rollback()
            log.exception("Evaluation failed for lead %s", lead.id)
    return done


async def notify_applications(db: Session, bot: Bot) -> int:
    pending = db.scalars(select(Application).where(Application.status.in_(["PENDING_REVIEW", "APPROVED"]), Application.notified_at.is_(None)).order_by(Application.score.desc()).limit(20)).all()
    sent = 0
    for application in pending:
        lead = db.get(Lead, application.lead_id)
        proposal = db.get(Proposal, application.proposal_id) if application.proposal_id else None
        card = application_card(lead, application, proposal.price if proposal else None) if lead else ""
        if lead and await broadcast(bot, card, application_keyboard(lead, application)):
            application.notified_at = now()
            db.commit()
            sent += 1
    return sent
