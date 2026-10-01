import asyncio
import logging
import re

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from fastapi import HTTPException
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.main import conversion, create_project, dashboard, revenue, update_project
from app.models import Application, FollowUp, Lead, Project, Proposal
from app.schemas import LeadInput, ProjectInput, ProjectUpdate
from app.services import analyze, approve, create_lead, generate_proposal, mark_sent, transition
from app.services.ai_evaluator import (
    approve_application,
    autopilot_enabled,
    daily_status,
    edit_application,
    mark_application_sent,
    set_autopilot,
    skip_application,
)
from app.services.payments import manual_payment
from app.telegram_ui import (
    EDIT_MARKER,
    application_card,
    application_keyboard,
    make_bot,
    status_text,
)

log = logging.getLogger(__name__)
dp = Dispatcher()
_bot: Bot | None = None


def get_bot() -> Bot:
    global _bot
    if _bot is None:
        _bot = make_bot()
    return _bot


def allowed(message: Message | CallbackQuery) -> bool:
    ids = settings().allowed_ids()
    return bool(ids) and message.from_user is not None and message.from_user.id in ids


def buttons(lead: Lead) -> InlineKeyboardMarkup:
    actions = {"NEW": [("Analyze", "analyze"), ("Reject", "reject")], "ANALYSIS_PENDING": [("Analyze", "analyze"), ("Reject", "reject")], "ANALYZED": [("Generate proposal", "proposal"), ("Reject", "reject")], "QUALIFIED": [("Generate proposal", "proposal"), ("Reject", "reject")], "PROPOSAL_READY": [("View proposal", "view"), ("Approve", "approve"), ("Reject", "reject")], "PROPOSAL_APPROVED": [("View proposal", "view"), ("Mark sent", "sent")], "SENT": [("Mark replied", "replied"), ("Won", "won"), ("Lost", "lost")], "REPLIED": [("Won", "won"), ("Lost", "lost")], "WON": [("Create project", "project")]}
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=title, callback_data=f"{action}:{lead.id}")] for title, action in actions.get(lead.status, [])] or [[InlineKeyboardButton(text="Open", callback_data=f"view:{lead.id}")]])


@dp.message(Command("start", "help"))
async def help_handler(message: Message):
    if allowed(message):
        await message.answer("/status — сводка за день\n/autopilot on|off — автопилот\n/pay_project <project_id> <amount> — отметить оплату\n/dashboard /leads /newlead <title> | <description> /pending /proposals /projects /hours <project_id> <hours> /stats\nМожно просто вставить текст заказа.")


DASHBOARD_LABELS = {"lead_count": "Заказов всего", "new_today": "Новых сегодня", "qualified": "Квалифицировано", "proposal_count": "Откликов всего", "proposals_ready": "Черновиков на проверке", "sent": "Отправлено", "replies": "Ответов", "won": "Выиграно", "pipeline": "В работе (сумма откликов)", "revenue": "Выручка", "profit": "Прибыль", "hours": "Часов", "paid": "Оплачено"}
ACTIVE_LEAD_STATUSES = ["NEW", "ANALYSIS_PENDING", "ANALYZING", "ANALYZED", "QUALIFIED", "PROPOSAL_READY", "PROPOSAL_APPROVED", "SENT", "REPLIED", "WON"]
STATUS_RU = {"NEW": "новый", "ANALYSIS_PENDING": "ждёт оценки", "ANALYZING": "оценивается", "ANALYZED": "оценён", "QUALIFIED": "подходит", "PROPOSAL_READY": "отклик на проверке", "PROPOSAL_APPROVED": "отклик одобрен", "SENT": "отклик отправлен", "REPLIED": "клиент ответил", "WON": "выигран", "LOST": "проигран", "REJECTED": "отклонён", "ARCHIVED": "в архиве", "DRAFT": "черновик", "APPROVED": "одобрен"}


def _money(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ")


@dp.message(Command("dashboard"))
async def dashboard_handler(message: Message):
    if allowed(message):
        with SessionLocal() as db:
            data = dashboard(db, None)
        money = {"pipeline", "revenue", "profit", "paid"}
        lines = [f"{DASHBOARD_LABELS.get(k, k)}: {_money(v) if k in money else v}" for k, v in data.items()]
        await message.answer("📈 Дашборд\n\n" + "\n".join(lines))


@dp.message(Command("stats"))
async def stats_handler(message: Message):
    if allowed(message):
        with SessionLocal() as db:
            conv = conversion(db, None)
            rev = revenue(db, None)
        await message.answer("\n".join([
            "📊 Статистика",
            f"Заказов: {conv['lead_count']} · откликов отправлено: {conv['sent_count']}",
            f"Доля ответов: {conv['reply_rate']:.0%} · выигрышей от отправленных: {conv['win_rate']:.0%}",
            f"Средний чек: {_money(rev['average_project_value'])} · эффективная ставка: {_money(rev['effective_hourly_rate'])}/ч",
            *[f"{month}: выручка {_money(v['revenue'])}, прибыль {_money(v['profit'])}, {v['hours']:g} ч" for month, v in sorted(rev["by_month"].items())],
        ]))


@dp.message(Command("leads", "pending"))
async def leads_handler(message: Message):
    if not allowed(message):
        return
    text = message.text or ""
    pending = text.startswith("/pending")
    with SessionLocal() as db:
        query = select(Lead).order_by(Lead.id.desc()).limit(10)
        if pending:
            query = query.where(Lead.status.in_(["NEW", "ANALYSIS_PENDING", "QUALIFIED", "PROPOSAL_READY"]))
        elif text.partition(" ")[2].strip() != "all":
            # Active pipeline only; "/leads all" includes rejected and archived ones.
            query = query.where(Lead.status.in_(ACTIVE_LEAD_STATUSES))
        rows = db.scalars(query).all()
        for row in rows:
            budget = f"до {_money(row.budget_max)} {row.currency}" if row.budget_max else "не указан"
            await message.answer(f"#{row.id} {row.title}\nСтатус: {STATUS_RU.get(row.status, row.status)} · Бюджет: {budget}", reply_markup=buttons(row))
    if not rows:
        await message.answer("Активных заказов нет. Все заказы: /leads all")


@dp.message(Command("proposals"))
async def proposals_handler(message: Message):
    if allowed(message):
        with SessionLocal() as db:
            rows = db.scalars(select(Proposal).where(Proposal.status.in_(["DRAFT", "APPROVED", "SENT"])).order_by(Proposal.id.desc()).limit(10)).all()
            for proposal in rows:
                lead = db.get(Lead, proposal.lead_id)
                await message.answer(f"Отклик #{proposal.id} · {STATUS_RU.get(proposal.status, proposal.status)} · {_money(proposal.price)} {proposal.currency}\n{lead.title}\n\n{proposal.body[:3000]}", reply_markup=buttons(lead))
        if not rows:
            await message.answer("Актуальных откликов нет")


@dp.message(Command("projects"))
async def projects_handler(message: Message):
    if allowed(message):
        with SessionLocal() as db:
            rows = db.scalars(select(Project).order_by(Project.id.desc()).limit(10)).all()
            for project in rows:
                markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Complete project", callback_data=f"complete:{project.id}")]]) if project.status not in {"COMPLETED", "CANCELLED"} else None
                await message.answer(f"#{project.id} {project.name}: {project.status}\nEstimate: {project.estimated_hours:g}h · Actual: {project.actual_hours:g}h", reply_markup=markup)
        if not rows:
            await message.answer("No projects")


@dp.message(Command("hours"))
async def hours_handler(message: Message):
    if not allowed(message):
        return
    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.answer("Использование: /hours <id проекта> <фактические часы>")
        return
    try:
        project_id, hours = int(parts[1]), float(parts[2])
        with SessionLocal() as db:
            project = update_project(project_id, ProjectUpdate(actual_hours=hours), db, None)
        await message.answer(f"Project #{project_id}: actual hours {project['actual_hours']:g}")
    except Exception as error:
        log.exception("Updating project hours failed")
        await message.answer(str(error)[:300])


@dp.message(Command("newlead"))
async def newlead_handler(message: Message):
    if not allowed(message):
        return
    content = (message.text or "").partition(" ")[2].strip()
    if not content:
        await message.answer("Использование: /newlead название | описание заказа\nИли просто вставьте текст заказа сообщением.")
        return
    title, separator, description = content.partition("|")
    with SessionLocal() as db:
        lead, created = create_lead(db, LeadInput(title=title.strip()[:500], description=(description if separator else content).strip()[:20000], source="telegram"))
    await message.answer(f"Lead #{lead.id} {'created' if created else 'already exists'}", reply_markup=buttons(lead))


@dp.message(Command("status"))
async def status_handler(message: Message):
    if allowed(message):
        with SessionLocal() as db:
            data = daily_status(db)
        await message.answer(status_text(data), parse_mode="HTML")


@dp.message(Command("autopilot"))
async def autopilot_handler(message: Message):
    if not allowed(message):
        return
    argument = (message.text or "").partition(" ")[2].strip().lower()
    with SessionLocal() as db:
        if argument in {"on", "off"}:
            set_autopilot(db, argument == "on")
        enabled = autopilot_enabled(db)
    cfg = settings()
    if enabled:
        detail = f"Заказы с оценкой ≥ {cfg.auto_apply_score}/10 одобряются автоматически и приходят готовыми к отправке."
    else:
        detail = f"Все заказы с оценкой ≥ {cfg.review_score}/10 приходят на проверку."
    usage = "" if argument in {"on", "off"} else "\nИспользование: /autopilot on|off"
    await message.answer(f"Автопилот {'🟢 включён' if enabled else '⚪️ выключен'}. {detail}{usage}")


@dp.message(Command("pay_project"))
async def pay_project_handler(message: Message):
    if not allowed(message):
        return
    parts = (message.text or "").split()
    try:
        project_id, amount = int(parts[1]), float(parts[2].replace(",", "."))
    except (IndexError, ValueError):
        await message.answer("Использование: /pay_project <project_id> <amount>")
        return
    try:
        with SessionLocal() as db:
            payment = manual_payment(db, project_id, amount)
            project = db.get(Project, project_id)
            text = f"💰 Проект #{project_id} оплачен: {payment.amount:g} {payment.currency} (всего {project.paid_amount:g} из {project.contract_price:g}). Итоговый отчёт формируется и придёт сюда."
    except HTTPException as error:
        text = f"Не удалось: {error.detail}"
    await message.answer(text)


@dp.message(F.text, F.reply_to_message.text.startswith(EDIT_MARKER))
async def edit_reply_handler(message: Message):
    if not allowed(message):
        return
    match = re.search(r"#A(\d+)", message.reply_to_message.text or "")
    if not match:
        return
    with SessionLocal() as db:
        application = db.get(Application, int(match.group(1)))
        if application is None:
            await message.answer("Заявка не найдена")
            return
        try:
            edit_application(db, application, message.text or "")
        except HTTPException as error:
            await message.answer(f"Не удалось: {error.detail}")
            return
        lead = db.get(Lead, application.lead_id)
        proposal = db.get(Proposal, application.proposal_id) if application.proposal_id else None
        await message.answer(application_card(lead, application, proposal.price if proposal else None), parse_mode="HTML", reply_markup=application_keyboard(lead, application))


@dp.callback_query(F.data.regexp(r"^(approve|edit|reject|sent)_\d+$"))
async def application_handler(callback: CallbackQuery):
    if not allowed(callback):
        return
    action, raw_id = callback.data.split("_")
    with SessionLocal() as db:
        application = db.get(Application, int(raw_id))
        if application is None:
            await callback.answer("Заявка не найдена")
            return
        lead = db.get(Lead, application.lead_id)
        try:
            if action == "edit":
                if application.status != "PENDING_REVIEW":
                    raise HTTPException(409, "Редактировать можно только до одобрения")
                await callback.message.answer(f"{EDIT_MARKER}{application.id} — ответьте на это сообщение новым текстом.", reply_markup=ForceReply(selective=True))
                await callback.answer()
                return
            if action == "approve":
                approve_application(db, application)
                note = "Одобрено — отправьте отклик на площадке"
            elif action == "sent":
                mark_application_sent(db, application)
                note = "Отмечено как отправленное, follow-up запланирован"
            else:
                skip_application(db, application)
                note = "Пропущено"
        except HTTPException as error:
            await callback.answer(str(error.detail)[:180], show_alert=True)
            return
        proposal = db.get(Proposal, application.proposal_id) if application.proposal_id else None
        text = application_card(lead, application, proposal.price if proposal else None)
        if application.status in {"SENT", "SKIPPED"}:
            text = ("✅ Отправлено" if application.status == "SENT" else "⏭ Пропущено") + "\n\n" + text
        try:
            await callback.message.edit_text(text[:4096], parse_mode="HTML", reply_markup=application_keyboard(lead, application))
        except TelegramBadRequest:
            log.warning("Could not update application card %s", application.id)
        await callback.answer(note)


@dp.message(F.text)
async def text_handler(message: Message):
    if not allowed(message):
        return
    text = (message.text or "").strip()
    if len(text) < 40 or text.startswith("/"):
        return
    await message.answer("Save this as a lead?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Save lead", callback_data="save_text")]]))
    pending_text[message.from_user.id] = text[:20000]


pending_text: dict[int, str] = {}


@dp.callback_query(F.data == "save_text")
async def save_text(callback: CallbackQuery):
    if not allowed(callback):
        return
    content = pending_text.pop(callback.from_user.id, None)
    if not content:
        await callback.answer("Text expired")
        return
    with SessionLocal() as db:
        lead, created = create_lead(db, LeadInput(title=content.splitlines()[0][:500], description=content, source="telegram"))
    await callback.message.answer(f"Lead #{lead.id} {'saved' if created else 'already exists'}", reply_markup=buttons(lead))
    await callback.answer()


@dp.callback_query(F.data.regexp(r"^(analyze|proposal|view|approve|sent|reject|replied|won|lost|project|complete|ignore):\d+$"))
async def action_handler(callback: CallbackQuery):
    if not allowed(callback):
        return
    action, raw_id = callback.data.split(":")
    with SessionLocal() as db:
        if action == "complete":
            try:
                project = update_project(int(raw_id), ProjectUpdate(status="COMPLETED"), db, None)
                await callback.message.answer(f"Project #{project['id']} completed. Postmortem: {project['postmortem']}")
                await callback.answer()
            except Exception as error:
                log.exception("Completing project failed")
                await callback.answer(str(error)[:180], show_alert=True)
            return
        if action == "ignore":
            followup = db.get(FollowUp, int(raw_id))
            if followup:
                followup.status = "IGNORED"
                db.commit()
            await callback.answer("Reminder ignored")
            return
        lead = db.get(Lead, int(raw_id))
        if not lead:
            await callback.answer("Lead not found")
            return
        try:
            if action == "analyze":
                result = await analyze(db, lead)
                output = f"Fit {result.fit_score}; profit {result.profit_score}; risk {result.risk_score}; score {result.overall_score}; price {result.recommended_price:g} {lead.currency}; action {result.recommended_action}"
            elif action == "proposal":
                output = (await generate_proposal(db, lead)).body
            elif action == "view":
                proposal = db.scalar(select(Proposal).where(Proposal.lead_id == lead.id).order_by(Proposal.id.desc()))
                output = proposal.body if proposal else "No proposal"
            elif action == "approve":
                approve(db, lead)
                output = "Approved. Copy the proposal and send it manually; then mark sent."
            elif action == "sent":
                mark_sent(db, lead)
                output = "Marked sent. Follow-up scheduled."
            elif action == "project":
                project = create_project(ProjectInput(lead_id=lead.id), db, None)
                output = f"Project #{project['id']} created. Use /projects to track it."
            else:
                target = {"reject": "REJECTED", "replied": "REPLIED", "won": "WON", "lost": "LOST"}[action]
                transition(lead, target)
                if target == "REPLIED":
                    followups = db.scalars(select(FollowUp).where(FollowUp.lead_id == lead.id, FollowUp.status == "PENDING")).all()
                    for followup in followups:
                        followup.status = "DONE"
                db.commit()
                output = f"Lead #{lead.id}: {target}"
            await callback.message.answer(output[:4000], reply_markup=buttons(lead))
            await callback.answer()
        except Exception as error:
            log.exception("Bot action failed")
            await callback.answer(str(error)[:180], show_alert=True)


async def main():
    """Long polling for local development. In production (TELEGRAM_MODE=webhook) the API serves
    updates at /api/v1/telegram/webhook and this process is not needed."""
    if not settings().telegram_bot_token or settings().telegram_mode != "polling":
        log.warning("Bot polling disabled (no token or TELEGRAM_MODE=%s); bot idle", settings().telegram_mode)
        while True:
            await asyncio.sleep(3600)
    bot = get_bot()
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
