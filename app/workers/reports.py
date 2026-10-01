"""Final client report for paid projects. Deterministic: built only from recorded project data,
without internal economics (hours, profit, notes)."""
import logging
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Client, Payment, Project

log = logging.getLogger(__name__)


def _date(value) -> str:
    return value.strftime("%d.%m.%Y") if value else "—"


def render_report(db: Session, project: Project) -> str:
    client = db.get(Client, project.client_id) if project.client_id else None
    payments = db.scalars(select(Payment).where(Payment.project_id == project.id, Payment.status == "APPLIED").order_by(Payment.id)).all()
    lines = [
        f"# Итоговый отчёт по проекту «{project.name}»",
        "",
        f"**Заказчик:** {client.name if client else '—'}",
        f"**Период работ:** {_date(project.start_date)} — {_date(project.completed_at)}",
        "",
        "## Объём работ",
        "",
        project.scope or "—",
    ]
    if project.requirements:
        lines += ["", "## Выполненные требования", ""] + [f"- {item}" for item in project.requirements]
    lines += [
        "",
        "## Финансы",
        "",
        f"- Стоимость по договорённости: {project.contract_price:g} {project.currency}",
        f"- Оплачено: {project.paid_amount:g} {project.currency}",
    ]
    lines += [f"  - {_date(p.created_at)}: {p.amount:g} {p.currency} ({p.provider})" for p in payments]
    lines += ["", "Спасибо за сотрудничество!", ""]
    return "\n".join(lines)


def build_pending_reports(db: Session) -> list[Project]:
    directory = Path(settings().reports_dir)
    directory.mkdir(parents=True, exist_ok=True)
    ready = []
    for project in db.scalars(select(Project).where(Project.report_status == "PENDING").limit(20)).all():
        try:
            path = directory / f"project-{project.id}-report.md"
            path.write_text(render_report(db, project), encoding="utf-8")
            project.report_path = str(path)
            project.report_status = "READY"
            db.commit()
            ready.append(project)
        except Exception:
            db.rollback()
            log.exception("Report generation failed for project %s", project.id)
    return ready


async def deliver_reports(db: Session, bot: Bot):
    for project in db.scalars(select(Project).where(Project.report_status == "READY").limit(20)).all():
        delivered = False
        for user_id in settings().allowed_ids():
            try:
                await bot.send_document(user_id, FSInputFile(project.report_path), caption=f"📄 Итоговый отчёт по проекту #{project.id} «{project.name[:100]}» — можно переслать клиенту.")
                delivered = True
            except Exception:
                log.exception("Report delivery failed for project %s", project.id)
        if delivered:
            project.report_status = "SENT"
            db.commit()
