"""One-click proposals for orders found in Telegram channels: the proposal is sent as a private
message to the client's contact from the freelancer's own Telegram account (Telethon user session).

Only on an explicit button press, one message per order, with a daily cap and a pause between
messages so the account is not flagged for spam.
"""
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, settings
from app.models import Activity, Lead, now

log = logging.getLogger(__name__)

MENTION_RE = re.compile(r"(?<![\w/@])@([A-Za-z][A-Za-z0-9_]{4,31})\b")
LINK_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]{4,31})(?![\w/])", re.IGNORECASE)
# Words that introduce the client's contact; a mention right after them wins over other mentions.
CONTACT_HINT_RE = re.compile(
    r"(?:писать|пишите|напишите|связь|контакт\w*|тг|telegram|телеграм\w*|в лс|в личку|лс|dm|contact|write to|message)"
    r"[^\n@]{0,25}@([A-Za-z][A-Za-z0-9_]{4,31})",
    re.IGNORECASE,
)
SOURCE_GROUP_RE = re.compile(r"(?:Групп[аы]|Group|Канал|Channel|Источник|Source)\s*:\s*@([A-Za-z0-9_]+)", re.IGNORECASE)


def find_contact(text: str, exclude: set[str] | None = None, extra: list[str] | None = None) -> str | None:
    """Best guess of the client's Telegram username in an order post, or None.

    Skips the channel/group the post came from, bots and known feeds; prefers a mention placed
    after words like «пишите», «контакт», «в лс»."""
    excluded = {x.lower() for x in (exclude or set())}
    excluded |= {m.lower() for m in SOURCE_GROUP_RE.findall(text)}

    def usable(name: str) -> bool:
        lowered = name.lower()
        return lowered not in excluded and not lowered.endswith("bot")

    hinted = [m for m in CONTACT_HINT_RE.findall(text) if usable(m)]
    if hinted:
        return hinted[0]
    candidates = [m for m in [*MENTION_RE.findall(text), *LINK_RE.findall(text), *(extra or [])] if usable(m)]
    return candidates[0] if candidates else None


def lead_contact(lead: Lead, cfg: Settings | None = None) -> str | None:
    """Contact for leads scraped from Telegram channels; other sources are answered on the platform."""
    payload = lead.raw_payload or {}
    # Kwork mirrors (it_zakazy) are answered on Kwork: the client has no Telegram contact in the post.
    if payload.get("source_platform") != "telegram" or lead.source == "kwork":
        return None
    cfg = cfg or settings()
    channels = {c.strip().lstrip("@") for c in cfg.telegram_channels.split(",") if c.strip()}
    excluded = channels | {payload.get("channel", "")} | set(payload.get("channel_handles") or [])
    return find_contact(lead.description, exclude=excluded, extra=payload.get("contacts") or [])


def outreach_ready(cfg: Settings | None = None) -> bool:
    cfg = cfg or settings()
    return bool(cfg.tg_outreach_enabled and cfg.tg_user_api_id and cfg.tg_user_api_hash and cfg.tg_user_session)


def compose_message(lead: Lead, proposal_body: str) -> str:
    """The client did not write to us first, so the message says which order it answers."""
    return f"Здравствуйте! Пишу по вашему заказу «{lead.title[:120]}».\n\n{proposal_body.strip()}"


@dataclass
class SendResult:
    ok: bool
    detail: str


class OutreachLimitError(Exception):
    pass


def check_limits(db: Session, cfg: Settings, current: datetime | None = None):
    current = current or now()
    sent = db.scalars(select(Activity.created_at).where(Activity.action == "outreach_sent", Activity.created_at >= current - timedelta(days=1)).order_by(Activity.created_at.desc())).all()
    if len(sent) >= cfg.tg_outreach_daily_limit:
        raise OutreachLimitError(f"Дневной лимит {cfg.tg_outreach_daily_limit} сообщений исчерпан — отправьте вручную")
    if sent:
        last = sent[0] if sent[0].tzinfo else sent[0].replace(tzinfo=current.tzinfo)
        wait = cfg.tg_outreach_min_interval_seconds - (current - last).total_seconds()
        if wait > 0:
            raise OutreachLimitError(f"Пауза между сообщениями: попробуйте через {int(wait) + 1} сек")


def proxy_options(proxy_url: str) -> dict:
    """socks5://user:pass@host:port, http://host:port or mtproxy://SECRET@host:port -> TelegramClient kwargs."""
    if not proxy_url:
        return {}
    match = re.fullmatch(r"(socks5|socks4|http|mtproxy)://(?:([^:@]*)(?::([^@]*))?@)?([^:/]+):(\d+)/?", proxy_url.strip())
    if not match:
        raise ValueError("TG_USER_PROXY: expected socks5://, http:// or mtproxy://SECRET@host:port")
    kind, user, password, host, port = match.groups()
    if kind == "mtproxy":
        from telethon import connection

        return {"connection": connection.ConnectionTcpMTProxyRandomizedIntermediate, "proxy": (host, int(port), user or "")}
    proxy = {"proxy_type": kind, "addr": host, "port": int(port), "rdns": True}
    if user:
        proxy.update(username=user, password=password or "")
    return {"proxy": proxy}


async def send_message(username: str, text: str, cfg: Settings | None = None) -> SendResult:
    """Send `text` to @username from the user's account. Errors are returned, not raised."""
    cfg = cfg or settings()
    from telethon import TelegramClient, errors
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(cfg.tg_user_session), cfg.tg_user_api_id, cfg.tg_user_api_hash,
                            **proxy_options(cfg.tg_user_proxy))
    started = time.monotonic()
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return SendResult(False, "Сессия Telegram недействительна — выполните tg-login заново")
        await client.send_message(username, text)
        return SendResult(True, f"Отправлено @{username} за {time.monotonic() - started:.1f} с")
    except errors.FloodWaitError as error:
        return SendResult(False, f"Telegram просит подождать {error.seconds} сек — отправьте вручную")
    except errors.PeerFloodError:
        return SendResult(False, "Telegram ограничил сообщения незнакомым (PeerFlood) — отправьте вручную и сделайте паузу")
    except (errors.UsernameNotOccupiedError, errors.UsernameInvalidError, ValueError):
        return SendResult(False, f"Пользователь @{username} не найден — проверьте контакт в заказе")
    except errors.UserPrivacyRestrictedError:
        return SendResult(False, f"@{username} закрыл личные сообщения — откликнитесь вручную")
    except (errors.RPCError, OSError, ConnectionError) as error:
        log.exception("Telegram outreach failed")
        return SendResult(False, f"Ошибка Telegram: {type(error).__name__}")
    finally:
        await client.disconnect()


def sent_today(db: Session) -> int:
    since = now() - timedelta(days=1)
    return db.scalar(select(func.count()).select_from(Activity).where(Activity.action == "outreach_sent", Activity.created_at >= since)) or 0
