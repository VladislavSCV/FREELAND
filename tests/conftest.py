import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["LLM_PROVIDER"] = "mock"
os.environ["API_TOKEN"] = "test-token"
# Environment variables override .env: tests must never reach real Telegram, payment or scraping services.
import tempfile

for name in ("TELEGRAM_BOT_TOKEN", "ALLOWED_TELEGRAM_IDS", "TELEGRAM_ALLOWED_USER_IDS", "TELEGRAM_WEBHOOK_SECRET", "WEBHOOK_DOMAIN", "PRODAMUS_SECRET_KEY", "YOOKASSA_SHOP_ID", "YOOKASSA_SECRET_KEY", "BRIGHTDATA_PROXY_URL", "BRIGHTDATA_TOKEN", "TELEGRAM_CHANNELS"):
    os.environ[name] = ""
os.environ["REPORTS_DIR"] = tempfile.mkdtemp(prefix="freelance-reports-")
os.environ["AUTOPILOT_ENABLED"] = "false"
os.environ["MANUAL_PAYMENT_MODE"] = "true"
os.environ["DEFAULT_CURRENCY"] = "USD"
os.environ["CURRENCY_RATES"] = "RUB=90,EUR=0.92"
os.environ["DEFAULT_HOURLY_RATE"] = "45"
os.environ["MIN_BUDGET"] = "0"
os.environ["TELEGRAM_API_URL"] = ""
os.environ["TELEGRAM_PROXY"] = ""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models
from app.db import Base, get_db
from app.main import app


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(engine, expire_on_commit=False)
    with session() as db:
        yield db
    engine.dispose()


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        test_client.headers["Authorization"] = "Bearer test-token"
        yield test_client
    app.dependency_overrides.clear()
