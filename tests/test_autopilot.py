import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.config import settings
from app.models import AgencyProfile, Application, FollowUp, Lead, Project, Service
from app.schemas import LeadInput
from app.services import convert, create_lead, score_10
from app.services.ai_evaluator import (
    approve_application,
    autopilot_enabled,
    daily_status,
    edit_application,
    evaluate,
    mark_application_sent,
    set_autopilot,
    skip_application,
)
from app.workers.pipeline import evaluate_pending
from app.workers.scraper import (
    FlRssSource,
    KworkSource,
    TelegramChannelSource,
    parse_budget,
    scrape,
    store,
)

NOW = datetime.now(UTC)
FL_RSS = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>FL.ru</title>
<item>
  <title><![CDATA[Telegram-бот для записи клиентов (Бюджет: 15000 &#8381;)]]></title>
  <link>https://www.fl.ru/projects/5512345/telegram-bot.html</link>
  <guid>https://www.fl.ru/projects/5512345/telegram-bot.html</guid>
  <description><![CDATA[Нужен бот на aiogram с <b>оплатой</b> и CRM.<br/>Срок 2 недели.]]></description>
  <category>Программирование</category>
  <pubDate>{NOW.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>
</item>
<item>
  <title>Старый заказ</title>
  <link>https://www.fl.ru/projects/5000001/old.html</link>
  <description>Очень старый заказ на парсер сайтов</description>
  <pubDate>Mon, 01 Jan 2024 10:00:00 +0000</pubDate>
</item>
</channel></rss>"""
TG_PAGE = f"""<html><body>
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message js-widget_message" data-post="freelance_orders/101">
<div class="tgme_widget_message_text js-message_text" dir="auto">#заказ<br/>Нужно настроить n8n и интеграцию с amoCRM для заявок с сайта.<br/>Бюджет: 30к руб</div>
<a class="tgme_widget_message_date" href="https://t.me/freelance_orders/101"><time datetime="{NOW.isoformat()}" class="time">10:00</time></a>
</div></div>
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message js-widget_message" data-post="freelance_orders/102">
<div class="tgme_widget_message_text js-message_text" dir="auto">Реклама</div>
</div></div>
</body></html>"""
KWORK_PAGE = """<html><script>window.stateData={"wantsListData":{"pagination":{"data":[
{"id":2900001,"name":"Чат-бот с GPT для поддержки","description":"Подключить OpenAI к Telegram-боту, база знаний из Google Docs","priceLimit":"20000.00","date_create":"2026-10-01 09:00:00"}
]}}};</script></html>"""


@pytest.fixture
def catalog(db):
    db.add(Service(category="TELEGRAM_BOT", name="Telegram bot", base_price=450, min_price=250, max_price=4000, typical_hours=10))
    db.add(AgencyProfile(name="Agency", description="Builds bots", skills=["Python", "n8n"], technologies=[]))
    db.commit()


@pytest.fixture
def cfg(monkeypatch):
    current = settings()
    for key, value in {"autopilot_enabled": False, "auto_apply_score": 8, "review_score": 5, "manual_payment_mode": True}.items():
        monkeypatch.setattr(current, key, value)
    return current


def lead(db, title="Telegram bot for bookings", description="Need a Telegram bot for bookings with payments"):
    row, _ = create_lead(db, LeadInput(title=title, description=description, source="test"))
    return row


def test_parse_budget():
    assert parse_budget("Бюджет: 15 000 руб.") == (15000, "RUB")
    assert parse_budget("Оплата: 30к ₽") == (30000, "RUB")
    assert parse_budget("Budget: $500") == (500, "USD")
    assert parse_budget("Бюджет: 5000\n2 недели") == (5000, None)
    assert parse_budget("без бюджета") == (None, None)


def test_fl_rss_parser():
    items = list(FlRssSource("x").parse(FL_RSS))
    assert items[0].external_id == "5512345"
    assert items[0].title == "Telegram-бот для записи клиентов"
    assert items[0].budget_max == 15000 and items[0].currency == "RUB"
    assert "оплатой" in items[0].description and "<b>" not in items[0].description
    assert items[0].raw_payload["source_platform"] == "fl_ru"


def test_telegram_parser_skips_short_posts():
    items = list(TelegramChannelSource("@freelance_orders").parse(TG_PAGE))
    assert len(items) == 1
    assert items[0].source == "tg_freelance_orders"
    assert items[0].external_id == "freelance_orders/101"
    assert items[0].url == "https://t.me/freelance_orders/101"
    assert items[0].budget_max == 30000
    assert items[0].title.startswith("заказ")


def test_kwork_parser_finds_projects_by_shape():
    items = list(KworkSource("x").parse(KWORK_PAGE))
    assert len(items) == 1
    assert items[0].external_id == "2900001" and items[0].budget_max == 20000
    assert items[0].url == "https://kwork.ru/projects/2900001"


def test_keyword_filter():
    from app.workers.scraper import KeywordFilter

    keywords = KeywordFilter("бот,python,ии,n8n", "логотип,прозвон")
    def item(title, text="описание задачи"):
        return LeadInput(source="t", title=title, description=text)
    assert keywords.accepts(item("Telegram-бот для записи"))
    assert keywords.accepts(item("Интеграция", "Нужен n8n сценарий"))
    assert keywords.accepts(item("ИИ-агент для amoCRM"))
    assert not keywords.accepts(item("Работа в офисе"))
    assert not keywords.accepts(item("Компания ищет менеджера по продажам линии"))
    assert not keywords.accepts(item("Прозвонить базу по скрипту", "бот не нужен"))
    assert not keywords.accepts(item("Логотип для кофейни", "нужен бот? нет"))
    assert keywords.accepts(item("Python-скрипт загрузки на YouTube, логотип не нужен"))
    assert KeywordFilter("", "").accepts(item("Что угодно"))


def test_store_applies_keyword_filter(db):
    from app.workers.scraper import KeywordFilter

    items = [LeadInput(source="t", external_id="1", title="Python парсер", description="Собрать данные"), LeadInput(source="t", external_id="2", title="Логотип", description="Нужен логотип")]
    assert store(db, items, 48, KeywordFilter("python", "логотип")) == 1


def test_store_dedups_and_skips_old(db):
    items = list(FlRssSource("x").parse(FL_RSS))
    assert store(db, items, max_age_hours=48) == 1
    assert store(db, items, max_age_hours=48) == 0
    stored = db.scalars(select(Lead)).all()
    assert len(stored) == 1 and stored[0].status == "NEW" and stored[0].content_hash


def test_same_text_from_two_channels_is_one_lead(db):
    text = "Нужен парсер маркетплейса с выгрузкой в Google Sheets и уведомлениями в Telegram"
    first, created = create_lead(db, LeadInput(source="tg_a", external_id="a/1", title="Парсер", description=text))
    second, created_again = create_lead(db, LeadInput(source="tg_b", external_id="b/9", title="Парсер", description=text))
    assert created and not created_again and first.id == second.id


def test_scraped_posts_with_generic_titles_are_not_merged(db):
    _, one = create_lead(db, LeadInput(source="tg_a", external_id="a/1", title="Ищу разработчика", description="Нужен лендинг для кофейни"))
    _, two = create_lead(db, LeadInput(source="tg_a", external_id="a/2", title="Ищу разработчика", description="Нужна интеграция n8n с Битрикс24"))
    assert one and two


def test_scrape_survives_failing_source(db):
    class Fetcher:
        async def get(self, url, source=""):
            if "t.me" in url:
                raise RuntimeError("blocked")
            return FL_RSS

    stats = asyncio.run(scrape(db, [FlRssSource("https://fl"), TelegramChannelSource("x")], Fetcher()))
    assert stats == {"fl_ru": 1, "tg_x": -1}


def test_score_scale_and_currency():
    assert score_10(68.75) == 7 and score_10(3) == 1 and score_10(100) == 10
    assert convert(100, "USD") == 100
    assert convert(100, "RUB") == 9000
    assert convert(100, "JPY") is None


def test_review_band_creates_pending_application(db, catalog, cfg):
    application = asyncio.run(evaluate(db, lead(db)))
    assert application.score == 7
    assert application.status == "PENDING_REVIEW"
    assert application.proposal_draft and application.match_reason
    assert db.get(Lead, application.lead_id).status == "PROPOSAL_READY"


def test_high_score_with_autopilot_is_auto_approved(db, catalog, cfg, monkeypatch):
    monkeypatch.setattr(cfg, "auto_apply_score", 7)
    set_autopilot(db, True)
    application = asyncio.run(evaluate(db, lead(db)))
    assert application.status == "APPROVED" and application.decision == "auto"
    assert db.get(Lead, application.lead_id).status == "PROPOSAL_APPROVED"


def test_high_score_without_autopilot_goes_to_review(db, catalog, cfg, monkeypatch):
    monkeypatch.setattr(cfg, "auto_apply_score", 7)
    assert not autopilot_enabled(db)
    assert asyncio.run(evaluate(db, lead(db))).status == "PENDING_REVIEW"


def test_low_score_is_archived_without_proposal(db, catalog, cfg, monkeypatch):
    monkeypatch.setattr(cfg, "review_score", 8)
    application = asyncio.run(evaluate(db, lead(db)))
    assert application.status == "ARCHIVED" and not application.proposal_draft
    assert db.get(Lead, application.lead_id).status == "ARCHIVED"


def test_application_actions(db, catalog, cfg):
    application = asyncio.run(evaluate(db, lead(db)))
    with pytest.raises(HTTPException):
        edit_application(db, application, "short")
    edit_application(db, application, "Сделаю бота на aiogram с оплатой через ЮKassa за неделю.")
    with pytest.raises(HTTPException):
        mark_application_sent(db, application)
    approve_application(db, application)
    with pytest.raises(HTTPException):
        edit_application(db, application, "Слишком поздно править текст отклика после одобрения.")
    mark_application_sent(db, application)
    assert application.status == "SENT" and application.sent_at
    assert db.scalar(select(FollowUp).where(FollowUp.lead_id == application.lead_id))
    other = asyncio.run(evaluate(db, lead(db, "Landing bot", "Another Telegram bot request for a dental clinic")))
    skip_application(db, other)
    assert other.status == "SKIPPED" and db.get(Lead, other.lead_id).status == "REJECTED"
    status = daily_status(db)
    assert status["evaluated"] == 2 and status["sent"] == 1


def test_pipeline_resumes_after_proposal_failure(db, catalog, cfg, monkeypatch):
    row = lead(db)
    import app.services.ai_evaluator as evaluator
    original = evaluator.generate_proposal

    async def failing(*args):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(evaluator, "generate_proposal", failing)
    assert asyncio.run(evaluate_pending(db)) == 0
    assert db.scalar(select(Application).where(Application.lead_id == row.id)).status == "PROPOSAL_PENDING"
    monkeypatch.setattr(evaluator, "generate_proposal", original)
    assert asyncio.run(evaluate_pending(db)) == 1
    assert db.scalar(select(Application).where(Application.lead_id == row.id)).status == "PENDING_REVIEW"
    assert asyncio.run(evaluate_pending(db)) == 0


def test_lead_buttons_keep_application_in_sync(client, db, catalog, cfg):
    application = asyncio.run(evaluate(db, lead(db)))
    assert client.post(f"/api/v1/leads/{application.lead_id}/approve").status_code == 200
    assert client.post(f"/api/v1/leads/{application.lead_id}/mark-sent").status_code == 200
    db.refresh(application)
    assert application.status == "SENT"
    assert client.get("/api/v1/applications").json()[0]["status"] == "SENT"


def test_autopilot_api(client):
    assert client.post("/api/v1/autopilot/on").json() == {"enabled": True}
    assert client.get("/api/v1/autopilot").json()["enabled"] is True
    assert client.post("/api/v1/autopilot/maybe").status_code == 422


def test_followup_due_is_counted(db, catalog, cfg):
    row = lead(db)
    db.add(FollowUp(lead_id=row.id, due_at=datetime.now(UTC) - timedelta(hours=1)))
    db.commit()
    assert daily_status(db)["followups_due"] == 1


def test_cleanup_demo_and_archive_pending(client, db, catalog, cfg):
    from app.services.maintenance import archive_pending, cleanup_demo

    smoke, _ = create_lead(db, LeadInput(source="smoke", title="Smoke bot", description="Telegram bot smoke test"))
    for path in ("analyze", "proposal", "approve", "mark-sent", "outcome/WON"):
        assert client.post(f"/api/v1/leads/{smoke.id}/{path}").status_code == 200
    assert client.post("/api/v1/projects", json={"lead_id": smoke.id}).status_code == 200
    real = asyncio.run(evaluate(db, lead(db)))
    assert cleanup_demo(db) == {"leads": 1, "projects": 1, "proposals": 1}
    assert db.get(Lead, smoke.id) is None and not db.scalars(select(Project)).all()
    assert archive_pending(db) == 1
    db.refresh(real)
    assert real.status == "ARCHIVED" and db.get(Lead, real.lead_id).status == "REJECTED"


def test_unknown_client_score_and_threshold_alias(db, catalog, cfg, monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("AUTOPILOT_THRESHOLD", "6")
    assert Settings(_env_file=None).auto_apply_score == 6
    monkeypatch.delenv("AUTOPILOT_THRESHOLD")
    monkeypatch.setenv("AUTO_APPLY_SCORE", "9")
    assert Settings(_env_file=None).auto_apply_score == 9
    # Mock analysis: fit 75, profit 70, client 50, risk 25; the lead has no client data.
    application = asyncio.run(evaluate(db, lead(db)))
    assert application.score == score_10(75 * .47 + 70 * .33 + 75 * .20)


def test_price_follows_budget_but_not_below_minimum(db, catalog, cfg):
    from app.models import LeadAnalysis

    def price_for(budget):
        row, _ = create_lead(db, LeadInput(source="test", title=f"Telegram bot {budget}", description=f"Need a Telegram bot, budget {budget}", budget_max=budget))
        asyncio.run(evaluate(db, row))
        return db.scalar(select(LeadAnalysis).where(LeadAnalysis.lead_id == row.id)).recommended_price

    # Mock estimate: 10 h x 45 = 450 USD; TELEGRAM_BOT minimum 250.
    assert price_for(None) == 450
    assert price_for(300) == 300
    assert price_for(100) == 250
    assert price_for(5000) == 450


def test_proposal_links_and_demo(client, db, catalog, cfg):
    from app.services import strip_unknown_links

    assert strip_unknown_links("Демо: https://demo.example/bot. Ещё https://fake.example/x тут.", {"https://demo.example/bot"}) == "Демо: https://demo.example/bot. Ещё тут."
    assert strip_unknown_links("Без ссылок.", set()) == "Без ссылок."
    from app.services import with_demo
    assert with_demo("Текст.", "https://demo.example/bot") == "Текст.\n\nПример похожего решения: https://demo.example/bot"
    assert with_demo("Демо https://demo.example/bot", "https://demo.example/bot") == "Демо https://demo.example/bot"
    assert with_demo("Текст.", "") == "Текст."
    response = client.patch("/api/v1/services/TELEGRAM_BOT", json={"demo_url": "https://demo.example/bot"})
    assert response.status_code == 200 and response.json()["demo_url"] == "https://demo.example/bot"
    assert client.patch("/api/v1/services/TELEGRAM_BOT", json={"demo_url": "javascript:alert(1)"}).status_code == 422
    assert client.patch("/api/v1/services/TELEGRAM_BOT", json={"demo_url": ""}).json()["demo_url"] is None
    assert client.patch("/api/v1/services/NOPE", json={"demo_url": ""}).status_code == 404


def test_default_keywords_cover_devops_and_keep_existing():
    from app.config import Settings
    from app.workers.scraper import KeywordFilter

    defaults = Settings(_env_file=None)
    keywords = KeywordFilter(defaults.scraper_include_keywords, defaults.scraper_exclude_keywords)

    def ok(title, text="описание"):
        return keywords.accepts(LeadInput(source="t", title=title, description=text))
    for title in ["Настроить VPS и задеплоить проект", "Перенести сайт на хостинг", "Настройка CI/CD в GitLab", "Docker Compose для сервиса", "Развернуть Ollama на сервере", "Мониторинг Grafana + Prometheus", "Telegram-бот для записи", "Интеграция amoCRM со СДЭК", "Лендинг для школы"]:
        assert ok(title), title
    assert not ok("Логотип для кофейни")


def test_model_reject_archives_even_with_passing_score(db, catalog, cfg, monkeypatch):
    from app.llm import MockProvider

    original = MockProvider.structured_generate

    async def rejecting(self, system, data, schema):
        result = await original(self, system, data, schema)
        if "services" in data:
            result.recommended_action = "REJECT"
        return result

    monkeypatch.setattr(MockProvider, "structured_generate", rejecting)
    application = asyncio.run(evaluate(db, lead(db, "Обновить 1С на сервере", "Обновить платформу 1С на сервере и клиентах")))
    assert application.score >= cfg.review_score
    assert application.status == "ARCHIVED" and not application.proposal_draft


IT_ZAKAZY_PAGE = """<div class="tgme_widget_message js-widget_message" data-post="it_zakazy/501">
<div class="tgme_widget_message_text js-message_text" dir="auto">📁 Скрипты, боты и mini apps | 💰 200 000 ₽ | ⏰ 10 дн.<br/>Telegram-бот для записи с оплатой<br/>Нужен бот на aiogram с оплатой и админкой.<br/>📨 5 предл.<br/><a href="https://kwork.ru/projects/3299001/view">🔗 Открыть на Kwork</a></div>
<time datetime="2026-10-14T07:18:04+00:00"></time></div>
<div class="tgme_widget_message js-widget_message" data-post="it_zakazy/502">
<div class="tgme_widget_message_text js-message_text" dir="auto">📁 Скрипты, боты и mini apps | 💰 1 500 ₽ | ⏰ 2 дн.<br/>Простой парсер сайта<br/>Спарсить 100 товаров в таблицу, нужен python скрипт.</div>
<time datetime="2026-10-14T08:00:00+00:00"></time></div>"""


def test_kwork_mirror_channel_and_min_budget(db, monkeypatch):
    from app.workers.scraper import KeywordFilter, worth_it

    monkeypatch.setattr(settings(), "default_currency", "RUB")

    first, second = TelegramChannelSource("it_zakazy").parse(IT_ZAKAZY_PAGE)
    assert first.title == "Telegram-бот для записи с оплатой"
    assert (first.budget_max, first.currency) == (200000, "RUB")
    assert (first.source, first.external_id, first.url) == ("kwork", "3299001", "https://kwork.ru/projects/3299001")
    assert second.source == "tg_it_zakazy" and second.budget_max == 1500
    assert worth_it(first, 10000) and not worth_it(second, 10000) and worth_it(second, 0)
    no_budget = LeadInput(source="t", title="Бот", description="Нужен бот")
    assert worth_it(no_budget, 10000)
    keywords = KeywordFilter("бот,парс,python", "")
    assert store(db, [first, second], 10**6, keywords, min_budget=10000) == 1
    # The same project arriving from the Kwork feed is recognised as known.
    assert store(db, [LeadInput(source="kwork", external_id="3299001", title="Другой заголовок", description="Другой текст про бота")], 10**6, keywords) == 0


def test_titles_skip_aggregator_meta_lines_and_uah_budget():
    from app.workers.scraper import parse_budget, title_from

    repost = "💬 📁 Скрипты, боты и mini apps | 💰 200 000 ₽ | ⏰ 10 дн.\n👥 Группа: @it_zakazy\nРазработка ИИ-инструмента для саун\nТекст"
    assert title_from(repost) == "Разработка ИИ-инструмента для саун"
    assert title_from("🆕 NEW PROJECT ON LABORX\n📢 Google Workspace Setup Specialist Needed\n💰 Budget: $40") == "Google Workspace Setup Specialist Needed"
    assert title_from("Telegram-бот для записи\nописание") == "Telegram-бот для записи"
    assert parse_budget("Бюджет: 5000 грн") == (5000, "UAH")
    assert parse_budget("💰 Budget: $40") == (40, "USD")


def test_project_model_defaults(db):
    row = lead(db)
    project = Project(lead_id=row.id, name="x", service_category="TELEGRAM_BOT", contract_price=100, currency="USD", estimated_hours=1)
    db.add(project)
    db.commit()
    assert project.paid_amount == 0 and project.report_status is None


def test_brightdata_routes_only_selected_sources(monkeypatch):
    import httpx

    from app.workers.scraper import Fetcher

    cfg = settings()
    for key, value in {"brightdata_token": "bd-token", "brightdata_zone": "web_unlocker1", "brightdata_proxy_url": "", "brightdata_sources": "kwork"}.items():
        monkeypatch.setattr(cfg, key, value)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url), request.headers.get("authorization"), request.content))
        return httpx.Response(200, text="page")

    fetcher = Fetcher(cfg, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert asyncio.run(fetcher.get("https://kwork.ru/projects", "kwork")) == "page"
    assert asyncio.run(fetcher.get("https://www.fl.ru/rss/all.xml", "fl_rss")) == "page"
    unlocker, direct = calls
    assert unlocker[:3] == ("POST", "https://api.brightdata.com/request", "Bearer bd-token")
    assert b'"zone":"web_unlocker1"' in unlocker[3].replace(b" ", b"") and b"kwork.ru" in unlocker[3]
    assert direct[:3] == ("GET", "https://www.fl.ru/rss/all.xml", None)


def test_reevaluate_archived_and_budget_warning(db, catalog, cfg):
    from app.services.maintenance import archive_pending, reevaluate_archived
    from app.telegram_ui import application_card
    from app.workers.pipeline import evaluate_pending

    good = lead(db, "Интеграция amoCRM со СДЭК", "Нужен скрипт интеграции amoCRM и СДЭК, бюджет 2000")
    good.budget_max = 2000
    db.commit()
    first = asyncio.run(evaluate(db, good))
    archive_pending(db)
    assert reevaluate_archived(db, lambda row: "amocrm" in row.title.casefold()) == 1
    assert db.get(Lead, good.id).status == "NEW"
    assert asyncio.run(evaluate_pending(db)) == 1
    again = db.scalar(select(Application).where(Application.lead_id == good.id))
    assert again is not None and again.status == "PENDING_REVIEW" and first.status == "ARCHIVED"
    card = application_card(good, again, 48600)
    assert "Наша цена: 48600" in card and "выше бюджета" in card
    assert "выше бюджета" not in application_card(good, again, 2500)


def test_telegram_channels_use_mirror(monkeypatch):
    from app.workers.scraper import configured_sources

    cfg = settings()
    monkeypatch.setattr(cfg, "scraper_sources", "telegram")
    monkeypatch.setattr(cfg, "telegram_channels", "orders_ru")
    monkeypatch.setattr(cfg, "telegram_api_url", "https://tg.example.workers.dev/")
    (source,) = configured_sources(cfg)
    seen = []

    class Fetcher:
        async def get(self, url, source=""):
            seen.append(url)
            return TG_PAGE

    asyncio.run(source.fetch(Fetcher()))
    assert seen == ["https://tg.example.workers.dev/s/orders_ru"]


def test_channel_post_classification():
    from app.workers.channels import classify

    def item(text):
        return LeadInput(source="t", title=text.splitlines()[0], description=text)

    assert classify(item("Нужно сделать Telegram-бота\nБюджет 30 000 ₽, сроки 10 дней")) == "order"
    assert classify(item("Looking for a developer to build a scraper\nBudget: $500 fixed price")) == "order"
    assert classify(item("Вакансия: Senior DevOps\nПолная занятость, оклад 300к, ДМС")) == "vacancy"
    assert classify(item("Company name: Acme\nTitle: SRE\nGrades: senior\nRemote: Yes")) == "vacancy"
    assert classify(item("Гугл запустил новую нейросеть, рассказываем подробности")) == "other"
