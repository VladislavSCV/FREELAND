import asyncio
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import settings
from app.models import Lead, Payment, Project
from app.services.payments import manual_payment, prodamus_signature, project_id_from
from app.workers.reports import build_pending_reports

TOKEN = "123456:TEST-token"


@pytest.fixture
def project(db):
    lead = Lead(title="Bot", description="Telegram bot", source="test", status="WON")
    db.add(lead)
    db.flush()
    row = Project(lead_id=lead.id, name="Бот для записи", service_category="TELEGRAM_BOT", scope="Бот с оплатой", requirements=["Запись", "Оплата"], contract_price=30000, currency="RUB", estimated_hours=10, actual_hours=12, external_cost=1000, status="IN_PROGRESS")
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def telegram(monkeypatch):
    current = settings()
    monkeypatch.setattr(current, "telegram_bot_token", TOKEN)
    monkeypatch.setattr(current, "telegram_webhook_secret", "")
    monkeypatch.setattr(current, "telegram_allowed_user_ids", "42")
    return current


def update(user_id: int) -> dict:
    return {"update_id": 1, "message": {"message_id": 1, "date": 0, "chat": {"id": user_id, "type": "private"}, "from": {"id": user_id, "is_bot": False, "first_name": "U"}, "text": "/status"}}


def test_telegram_webhook_validates_secret_and_user(client, telegram, monkeypatch):
    fed = []

    async def feed(bot, upd):
        fed.append(upd.message.from_user.id)

    import app.bot
    monkeypatch.setattr(app.bot.dp, "feed_update", feed)
    url = "/api/v1/telegram/webhook"
    assert client.post(url, json=update(42)).status_code == 403
    assert client.post(url, json=update(42), headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"}).status_code == 403
    headers = {"X-Telegram-Bot-Api-Secret-Token": telegram.webhook_secret()}
    assert client.post(url, json=update(7), headers=headers).json() == {"ok": True}
    assert client.post(url, json=update(42), headers=headers).json() == {"ok": True}
    assert fed == [42]


def test_webhook_secret_derives_from_bot_token(telegram):
    assert len(telegram.webhook_secret()) == 64
    assert TOKEN not in telegram.webhook_secret()


def test_manual_payment_marks_paid_and_builds_report(db, project, tmp_path, monkeypatch):
    monkeypatch.setattr(settings(), "reports_dir", str(tmp_path))
    payment = manual_payment(db, project.id, 30000)
    assert payment.status == "APPLIED"
    assert project.status == "PAID" and project.paid_amount == 30000
    assert project.profit == 29000 and project.postmortem["ai_status"] == "PENDING"
    assert project.report_status == "PENDING"
    assert [p.id for p in build_pending_reports(db)] == [project.id]
    text = Path(project.report_path).read_text(encoding="utf-8")
    assert "Бот для записи" in text and "30000 RUB" in text and "Оплата" in text
    assert "profit" not in text.lower() and "час" not in text


def test_paid_project_is_closed_for_edits(client, db, project):
    manual_payment(db, project.id, 100)
    assert client.patch(f"/api/v1/projects/{project.id}", json={"actual_hours": 1}).status_code == 409
    assert client.get("/api/v1/analytics/dashboard").json()["paid"] == 100


def test_project_reference_parsing():
    assert project_id_from("project-12") == 12
    assert project_id_from("7") == 7
    assert project_id_from("order-7") is None
    assert project_id_from(None) is None


def prodamus_form(project_id: int) -> dict:
    return {"order_id": "9001", "order_num": f"project-{project_id}", "sum": "30000.00", "currency": "rub", "payment_status": "success", "customer_email": "a@b.ru", "products[0][name]": "Бот/разработка", "products[0][price]": "30000.00"}


def test_prodamus_webhook(client, db, project, monkeypatch):
    url = "/api/v1/webhooks/prodamus"
    form = prodamus_form(project.id)
    assert client.post(url, data=form).status_code == 503
    monkeypatch.setattr(settings(), "prodamus_secret_key", "secret")
    nested = {k: v for k, v in form.items() if not k.startswith("products")} | {"products": [{"name": "Бот/разработка", "price": "30000.00"}]}
    sign = prodamus_signature(nested, "secret")
    assert client.post(url, data=form, headers={"Sign": "bad"}).status_code == 403
    response = client.post(url, data=form, headers={"Sign": sign})
    assert response.status_code == 200
    assert response.json()["payment"]["status"] == "RECORDED"
    db.refresh(project)
    assert project.status == "IN_PROGRESS"
    assert client.post(url, data=form, headers={"Sign": sign}).json()["payment"]["id"] == response.json()["payment"]["id"]


def test_prodamus_applies_when_acquiring_enabled(client, db, project, monkeypatch):
    monkeypatch.setattr(settings(), "prodamus_secret_key", "secret")
    monkeypatch.setattr(settings(), "manual_payment_mode", False)
    form = prodamus_form(project.id)
    nested = {k: v for k, v in form.items() if not k.startswith("products")} | {"products": [{"name": "Бот/разработка", "price": "30000.00"}]}
    response = client.post("/api/v1/webhooks/prodamus", data=form, headers={"Sign": prodamus_signature(nested, "secret")})
    assert response.json()["payment"]["status"] == "APPLIED"
    db.refresh(project)
    assert project.status == "PAID"


def test_yookassa_webhook_verifies_via_api(client, db, project, monkeypatch):
    url = "/api/v1/webhooks/yookassa"
    body = {"type": "notification", "event": "payment.succeeded", "object": {"id": "2d6f-abc", "status": "succeeded"}}
    assert client.post(url, json=body).status_code == 503
    monkeypatch.setattr(settings(), "yookassa_shop_id", "shop")
    monkeypatch.setattr(settings(), "yookassa_secret_key", "key")
    monkeypatch.setattr(settings(), "manual_payment_mode", False)
    answers = {"2d6f-abc": {"id": "2d6f-abc", "status": "succeeded", "paid": True, "amount": {"value": "15000.00", "currency": "RUB"}, "metadata": {"project_id": str(project.id)}}, "fake": {"id": "fake", "status": "pending"}}

    async def fetch(payment_id):
        return answers[payment_id]

    monkeypatch.setattr("app.services.payments.fetch_yookassa_payment", fetch)
    assert client.post(url, json={**body, "event": "payment.waiting_for_capture"}).json()["payment"] is None
    assert client.post(url, json={**body, "object": {"id": "fake"}}).status_code == 409
    assert client.post(url, json=body).json()["payment"]["status"] == "APPLIED"
    assert client.post(url, json=body).status_code == 200
    db.refresh(project)
    assert project.status == "PAID" and project.paid_amount == 15000
    assert len(db.scalars(select(Payment)).all()) == 1


def test_yandex_provider_request(monkeypatch):
    import httpx

    from app.llm import extract_json, provider
    from app.schemas import PostmortemResult

    current = settings()
    for key, value in {"llm_provider": "yandex", "llm_model": "yandexgpt-lite/latest", "llm_base_url": "", "yandex_api_key": "AQVN-test", "yandex_folder_id": "b1gfolder"}.items():
        monkeypatch.setattr(current, key, value)
    sent = {}

    async def post(self, url, headers=None, json=None, **kwargs):
        sent.update(url=url, headers=headers, body=json)
        content = '```\n{"went_well": ["ok"], "went_wrong": [], "underestimated_areas": [], "overestimated_areas": [], "recommended_price_next_time": 100, "process_change": "none"}\n```'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}], "usage": {"prompt_tokens": 5, "completion_tokens": 7}}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    client = provider()
    result = asyncio.run(client.structured_generate("system", {"project": {}}, PostmortemResult))
    assert result.went_well == ["ok"] and client.usage == {"input_tokens": 5, "output_tokens": 7}
    assert sent["url"] == "https://ai.api.cloud.yandex.net/v1/chat/completions"
    assert sent["headers"] == {"Authorization": "Api-Key AQVN-test", "OpenAI-Project": "b1gfolder"}
    assert sent["body"]["model"] == "gpt://b1gfolder/yandexgpt-lite/latest"
    assert sent["body"]["response_format"]["type"] == "json_schema"
    schema = sent["body"]["response_format"]["json_schema"]["schema"]
    assert set(schema["required"]) == set(schema["properties"])
    assert extract_json('Ответ: {"a": 1} готово') == '{"a": 1}'
