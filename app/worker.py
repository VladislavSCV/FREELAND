import asyncio
import logging
import time

from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.llm import observed_generate
from app.models import FollowUp, Lead, Project, now
from app.schemas import PostmortemResult
from app.services import prompt
from app.workers.pipeline import evaluate_pending, notify_applications
from app.workers.reports import build_pending_reports, deliver_reports
from app.workers.scraper import scrape

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


async def tick():
    with SessionLocal() as db:
        await evaluate_pending(db)
        completed = db.scalars(select(Project).where(Project.status.in_(["COMPLETED", "PAID"])).order_by(Project.id.desc()).limit(100)).all()
        for project in completed:
            if project.postmortem.get("ai_status") != "PENDING":
                continue
            try:
                data = {"project": {"name": project.name, "scope": project.scope, "contract_price": project.contract_price, "currency": project.currency, "estimated_hours": project.estimated_hours, "actual_hours": project.actual_hours, "external_cost": project.external_cost, "profit": project.profit, "client_feedback": project.postmortem.get("client_feedback", "")}}
                result = await observed_generate(db, "postmortem", prompt(db, "postmortem"), data, PostmortemResult)
                project.postmortem = {**project.postmortem, "ai_status": "READY", "draft": result.model_dump()}
                db.commit()
            except Exception:
                log.exception("Postmortem generation failed for project %s", project.id)
        build_pending_reports(db)
        due = db.scalars(select(FollowUp).where(FollowUp.status == "PENDING", FollowUp.due_at <= now(), FollowUp.notified_at.is_(None)).limit(20)).all()
        if not settings().telegram_bot_token or not settings().allowed_ids():
            return
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        from app.telegram_ui import make_bot
        async with make_bot() as bot:
            await notify_applications(db, bot)
            await deliver_reports(db, bot)
            for followup in due:
                delivered = False
                for user_id in settings().allowed_ids():
                    try:
                        buttons = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Mark replied", callback_data=f"replied:{followup.lead_id}"), InlineKeyboardButton(text="Ignore", callback_data=f"ignore:{followup.id}")]])
                        await bot.send_message(user_id, f"Follow-up due for lead #{followup.lead_id}. Review client response manually.", reply_markup=buttons)
                        delivered = True
                    except Exception:
                        log.exception("Follow-up notification failed")
                if delivered:
                    followup.notified_at = now()
            db.commit()


def recover_interrupted():
    """A single worker runs analyses, so anything still ANALYZING at startup was interrupted."""
    with SessionLocal() as db:
        stale = db.scalars(select(Lead).where(Lead.status == "ANALYZING")).all()
        for lead in stale:
            lead.status = "ANALYSIS_PENDING"
        db.commit()
        if stale:
            log.info("Recovered %s interrupted analyses", len(stale))


async def main():
    recover_interrupted()
    last_scrape = 0.0
    while True:
        cfg = settings()
        if cfg.scraper_enabled and time.monotonic() - last_scrape >= cfg.scraper_interval_seconds:
            last_scrape = time.monotonic()
            try:
                with SessionLocal() as db:
                    log.info("Scrape finished: %s", await scrape(db))
            except Exception:
                log.exception("Scrape failed")
        try:
            await tick()
        except Exception:
            log.exception("Worker tick failed")
        await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main())
