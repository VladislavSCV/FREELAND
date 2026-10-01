"""Telegram message rendering and delivery shared by the bot (webhook/polling) and the worker."""
import html
import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

from app.config import settings
from app.models import Application, Lead

log = logging.getLogger(__name__)
EDIT_MARKER = "✏️ Новый текст отклика для заявки #A"


def make_bot() -> Bot:
    """Bot client honouring TELEGRAM_API_URL / TELEGRAM_PROXY (needed where Telegram is blocked)."""
    cfg = settings()
    if not cfg.telegram_api_url and not cfg.telegram_proxy:
        return Bot(cfg.telegram_bot_token)
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.client.telegram import TelegramAPIServer

    session = AiohttpSession(proxy=cfg.telegram_proxy or None)
    if cfg.telegram_api_url:
        session.api = TelegramAPIServer.from_base(cfg.telegram_api_url.rstrip("/"))
    return Bot(cfg.telegram_bot_token, session=session)


def _budget(lead: Lead) -> str:
    if lead.budget_min and lead.budget_max and lead.budget_min != lead.budget_max:
        return f"{lead.budget_min:g}–{lead.budget_max:g} {lead.currency}"
    value = lead.budget_max or lead.budget_min
    return f"{value:g} {lead.currency}" if value else "не указан"


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def application_card(lead: Lead, application: Application, price: float | None = None) -> str:
    ready = application.status == "APPROVED"
    header = "🚀 Готово к отправке (автопилот)" if ready and application.decision == "auto" else "🚀 Готово к отправке" if ready else "🟡 Нужна проверка"
    lines = [
        f"<b>{header}</b> · заявка #A{application.id} · оценка <b>{application.score}/10</b>",
        f"<b>{html.escape(_cut(lead.title, 200))}</b>",
        f"Источник: {html.escape(lead.source)} · Бюджет: {html.escape(_budget(lead))}" + (f" · Наша цена: {price:g} {lead.currency}" if price else ""),
        *([f"⚠️ Наша цена в {price / budget:.1f} раза выше бюджета клиента — проверьте, стоит ли откликаться."] if price and (budget := lead.budget_max or lead.budget_min) and price > budget * 1.5 else []),
        "",
        html.escape(_cut(lead.description, 1000)),
        "",
        f"🧠 <i>{html.escape(_cut(application.match_reason or '—', 400))}</i>",
        "",
        "<b>Отклик:</b>",
        f"<pre>{html.escape(_cut(application.proposal_draft, 2000))}</pre>",
    ]
    if ready:
        lines.append("Скопируйте текст, отправьте отклик на площадке и нажмите «Отправил».")
    return "\n".join(lines)


def application_keyboard(lead: Lead, application: Application) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if lead.url:
        rows.append([InlineKeyboardButton(text="🔗 Открыть заказ", url=lead.url)])
    if application.status == "PENDING_REVIEW":
        rows.append([
            InlineKeyboardButton(text="✅ Отправить", callback_data=f"approve_{application.id}"),
            InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"edit_{application.id}"),
            InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"reject_{application.id}"),
        ])
    elif application.status == "APPROVED":
        rows.append([
            InlineKeyboardButton(text="📨 Отправил", callback_data=f"sent_{application.id}"),
            InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"reject_{application.id}"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def status_text(data: dict) -> str:
    statuses = ", ".join(f"{k}: {v}" for k, v in sorted(data["by_status"].items())) or "—"
    return "\n".join([
        "<b>📊 Сводка за сегодня</b>",
        f"Автопилот: {'🟢 включён' if data['autopilot'] else '⚪️ выключен'}",
        f"Собрано заказов: {data['collected']}",
        f"Оценено: {data['evaluated']} ({statuses})",
        f"Одобрено автопилотом: {data['auto_approved']}",
        f"Отправлено откликов: {data['sent']}",
        f"Ждут проверки: {data['waiting_review']} · ждут отправки: {data['waiting_send']}",
        f"Follow-up к выполнению: {data['followups_due']}",
    ])


async def broadcast(bot: Bot, text: str, markup: InlineKeyboardMarkup | None = None) -> bool:
    delivered = False
    for user_id in settings().allowed_ids():
        try:
            await bot.send_message(user_id, text, reply_markup=markup, parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True))
            delivered = True
        except Exception:
            log.exception("Telegram delivery to %s failed", user_id)
    return delivered
