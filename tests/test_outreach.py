import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.models import Activity, AgencyProfile, Application, Lead, Service
from app.services.ai_evaluator import evaluate
from app.services.outreach import (
    OutreachLimitError,
    SendResult,
    check_limits,
    compose_message,
    find_contact,
    lead_contact,
    proxy_options,
)
from app.telegram_ui import application_keyboard


@pytest.mark.parametrize(("text", "expected"), [
    ("Нужен бот для записи. Пишите в лс @ivan_client", "ivan_client"),
    ("👥 Группа: @freelance_zakazy\nНужен парсер. Контакт: @maria_shop", "maria_shop"),
    ("👥 Группа: @freelance_zakazy\nНужен парсер, бюджет 20к", None),
    ("Заказ через @order_helper_bot, связь @real_person", "real_person"),
    ("Подробности: t.me/petr_dev_client", "petr_dev_client"),
    ("Нужен лендинг, email test@mail.ru", None),
    ("Бот для салона, пишите @salon_owner или @second_person", "salon_owner"),
])
def test_find_contact(text, expected):
    assert find_contact(text, exclude={"it_zakazy"}) == expected


def test_find_contact_uses_html_links_and_skips_channel():
    assert find_contact("Написать заказчику", exclude={"zakazy_it"}, extra=["client_from_link"]) == "client_from_link"
    assert find_contact("Источник: @zakazy_it", exclude={"zakazy_it"}) is None


def test_lead_contact_only_for_telegram_leads():
    tg = Lead(title="Бот", description="Нужен бот, пишите @client_one", source="tg_zakazy_it", raw_payload={"source_platform": "telegram", "channel": "zakazy_it"})
    kwork = Lead(title="Бот", description="Нужен бот, пишите @client_one", source="kwork", raw_payload={"source_platform": "kwork"})
    assert lead_contact(tg) == "client_one"
    assert lead_contact(kwork) is None


def test_proxy_options():
    assert proxy_options("") == {}
    socks = proxy_options("socks5://user:pa:ss@1.2.3.4:1080")["proxy"]
    assert socks == {"proxy_type": "socks5", "addr": "1.2.3.4", "port": 1080, "rdns": True, "username": "user", "password": "pa:ss"}
    mt = proxy_options("mtproxy://ee00ff@proxy.example:443")
    assert mt["proxy"] == ("proxy.example", 443, "ee00ff") and "MTProxy" in mt["connection"].__name__
    with pytest.raises(ValueError):
        proxy_options("ftp://host:21")


def test_limits(db, monkeypatch):
    cfg = settings()
    monkeypatch.setattr(cfg, "tg_outreach_daily_limit", 2)
    monkeypatch.setattr(cfg, "tg_outreach_min_interval_seconds", 60)
    current = datetime.now(UTC)
    check_limits(db, cfg, current)
    db.add(Activity(entity_type="application", entity_id=1, action="outreach_sent", details={}, created_at=current - timedelta(seconds=10)))
    db.commit()
    with pytest.raises(OutreachLimitError, match="Пауза"):
        check_limits(db, cfg, current)
    db.add(Activity(entity_type="application", entity_id=2, action="outreach_sent", details={}, created_at=current - timedelta(hours=1)))
    db.commit()
    with pytest.raises(OutreachLimitError, match="лимит"):
        check_limits(db, cfg, current + timedelta(minutes=5))


def test_compose_message_names_the_order():
    lead = Lead(title="Telegram-бот для салона", description="…")
    assert compose_message(lead, "Сделаю бота.").startswith("Здравствуйте! Пишу по вашему заказу «Telegram-бот для салона».")


@pytest.fixture
def outreach_env(db, monkeypatch):
    cfg = settings()
    for key, value in {"tg_outreach_enabled": True, "tg_user_api_id": 1, "tg_user_api_hash": "h", "tg_user_session": "s", "telegram_allowed_user_ids": "42",
                       "telegram_channels": "zakazy_it", "tg_outreach_min_interval_seconds": 0, "auto_apply_score": 8,
                       "review_score": 5, "autopilot_enabled": False}.items():
        monkeypatch.setattr(cfg, key, value)
    db.add(Service(category="TELEGRAM_BOT", name="Telegram bot", base_price=450, min_price=250, max_price=4000, typical_hours=10))
    db.add(AgencyProfile(name="Agency", description="Builds bots", skills=["Python"], technologies=[]))
    db.commit()
    lead = Lead(title="Telegram bot for bookings", description="Need a Telegram bot for bookings. Пишите @salon_owner",
                source="tg_zakazy_it", currency="USD", raw_payload={"source_platform": "telegram", "channel": "zakazy_it"})
    db.add(lead)
    db.commit()
    application = asyncio.run(evaluate(db, lead))

    @contextmanager
    def session():
        yield db

    monkeypatch.setattr("app.bot.SessionLocal", session)
    return db, lead, application


def callback(application_id):
    message = SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock())
    return SimpleNamespace(data=f"dm_{application_id}", from_user=SimpleNamespace(id=42), message=message, answer=AsyncMock())


def test_keyboard_shows_one_click_button(outreach_env):
    _, lead, application = outreach_env
    buttons = [b for row in application_keyboard(lead, application).inline_keyboard for b in row]
    assert buttons[0].text == "🚀 Откликнуться: написать @salon_owner" and buttons[0].callback_data == f"dm_{application.id}"


def test_one_click_sends_and_marks_sent(outreach_env, monkeypatch):
    import app.bot

    db, lead, application = outreach_env
    sent = AsyncMock(return_value=SendResult(True, "ok"))
    monkeypatch.setattr("app.services.outreach.send_message", sent)
    cb = callback(application.id)
    asyncio.run(app.bot.outreach_handler(cb))
    username, text = sent.await_args.args
    assert username == "salon_owner" and "Пишу по вашему заказу" in text and application.proposal_draft in text
    db.refresh(application)
    assert application.status == "SENT" and db.get(Lead, lead.id).status == "SENT"
    assert "Отклик отправлен в Telegram @salon_owner" in cb.message.edit_text.await_args.args[0]
    # A second press does nothing.
    asyncio.run(app.bot.outreach_handler(callback(application.id)))
    assert sent.await_count == 1


def test_one_click_failure_keeps_card_for_manual_send(outreach_env, monkeypatch):
    import app.bot

    db, _, application = outreach_env
    monkeypatch.setattr("app.services.outreach.send_message", AsyncMock(return_value=SendResult(False, "PeerFlood")))
    cb = callback(application.id)
    asyncio.run(app.bot.outreach_handler(cb))
    assert "Не отправлено: PeerFlood" in cb.message.answer.await_args.args[0]
    db.refresh(application)
    assert application.status == "PENDING_REVIEW"


def test_one_click_unavailable_without_session(outreach_env, monkeypatch):
    import app.bot

    _, lead, application = outreach_env
    monkeypatch.setattr(settings(), "tg_user_session", "")
    assert all(not b.callback_data or not b.callback_data.startswith("dm_")
               for row in application_keyboard(lead, application).inline_keyboard for b in row)
    cb = callback(application.id)
    asyncio.run(app.bot.outreach_handler(cb))
    assert "вручную" in cb.answer.await_args.args[0]
    assert db_status(outreach_env) == "PENDING_REVIEW"


def test_switch_off_hides_button_and_blocks_sending(outreach_env, monkeypatch):
    import app.bot

    _, lead, application = outreach_env
    monkeypatch.setattr(settings(), "tg_outreach_enabled", False)
    assert not any((b.callback_data or "").startswith("dm_") for row in application_keyboard(lead, application).inline_keyboard for b in row)
    sent = AsyncMock()
    monkeypatch.setattr("app.services.outreach.send_message", sent)
    asyncio.run(app.bot.outreach_handler(callback(application.id)))
    assert sent.await_count == 0 and db_status(outreach_env) == "PENDING_REVIEW"


def db_status(env) -> str:
    db, _, application = env
    db.refresh(application)
    return db.get(Application, application.id).status


def test_channel_signature_handles_are_not_contacts():
    from app.workers.scraper import TelegramChannelSource

    def post(n, text):
        return (f'<div class="tgme_widget_message" data-post="ch_orders/{n}"><div class="tgme_widget_message_text js-message_text">'
                f'{text}<br/>Реклама: пишите @channel_admin</div><time datetime="2026-10-14T10:00:00+00:00"></time></div>')

    page = "".join([post(1, "Нужен бот для записи, бюджет 30000 руб., пишите @real_client"),
                    post(2, "Нужен парсер сайта на python, бюджет 20000 руб."),
                    post(3, "Нужна интеграция amoCRM, бюджет 40000 руб.")])
    items = list(TelegramChannelSource("ch_orders").parse(page))
    leads = [Lead(title=i.title, description=i.description, source=i.source, raw_payload=i.raw_payload) for i in items]
    assert [lead_contact(lead) for lead in leads] == ["real_client", None, None]


def test_kwork_mirror_posts_have_no_dm_contact():
    lead = Lead(title="Бот", description="Нужен бот, пишите @someone_here", source="kwork",
                raw_payload={"source_platform": "telegram", "channel": "it_zakazy"})
    assert lead_contact(lead) is None


def test_scraper_keeps_profile_links_from_html():
    from app.workers.scraper import TelegramChannelSource

    page = """<div class="tgme_widget_message" data-post="zakazy_it/77">
<div class="tgme_widget_message_text js-message_text">Нужен бот для записи клиентов, бюджет 30000 руб.<br/>
<a href="https://t.me/client_hidden">Написать заказчику</a> <a href="https://t.me/zakazy_it/70">прошлый пост</a></div>
<time datetime="2026-10-14T10:00:00+00:00"></time></div>"""
    (item,) = TelegramChannelSource("zakazy_it").parse(page)
    assert item.raw_payload["contacts"] == ["client_hidden"]
    lead = Lead(title=item.title, description=item.description, source=item.source, raw_payload=item.raw_payload)
    assert lead_contact(lead) == "client_hidden"


def test_empty_api_id_in_env_means_not_set(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("TG_USER_API_ID", "")
    assert Settings(_env_file=None).tg_user_api_id == 0
