import hashlib
from functools import lru_cache

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    project_name: str = "Freelance OS"
    database_url: str = "sqlite:///./freelance.db"
    redis_url: str = "redis://localhost:6379/0"
    api_token: str = ""
    webhook_token: str = ""
    telegram_bot_token: str = ""
    telegram_allowed_user_ids: str = Field(default="", validation_alias=AliasChoices("ALLOWED_TELEGRAM_IDS", "TELEGRAM_ALLOWED_USER_IDS"))
    telegram_mode: str = "webhook"
    # For servers where api.telegram.org is unreachable: a Bot API mirror (e.g. a Cloudflare Worker)
    # and/or an outgoing proxy (http://user:pass@host:port or socks5://...).
    telegram_api_url: str = ""
    telegram_proxy: str = ""
    telegram_webhook_secret: str = ""
    webhook_domain: str = ""
    llm_provider: str = "mock"
    llm_model: str = "gpt-4o-mini"
    llm_base_url: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    deepseek_api_key: str = ""
    yandex_api_key: str = ""
    yandex_folder_id: str = ""
    default_currency: str = "USD"
    default_hourly_rate: float = 45
    currency_rates: str = "RUB=90,EUR=0.92"
    min_fit_score: int = 70
    min_profit_score: int = 60
    followup_days: int = 2
    score_fit_weight: float = 0.35
    score_profit_weight: float = 0.25
    score_client_weight: float = 0.20
    score_risk_weight: float = 0.20
    autopilot_enabled: bool = False
    auto_apply_score: int = Field(default=7, validation_alias=AliasChoices("AUTOPILOT_THRESHOLD", "AUTO_APPLY_SCORE"))
    review_score: int = 5
    max_evaluations_per_tick: int = 10
    max_analysis_attempts: int = 5
    scraper_enabled: bool = True
    scraper_interval_seconds: int = 600
    scraper_sources: str = "fl_rss,telegram"
    scraper_max_age_hours: int = 48
    # Comma-separated feeds; FL.ru category=5 is programming, category=2 is websites; Kwork c=11 is "Разработка и IT".
    fl_rss_url: str = "https://www.fl.ru/rss/all.xml?category=5,https://www.fl.ru/rss/all.xml?category=2"
    kwork_projects_url: str = "https://kwork.ru/projects?c=11"
    # Pre-filter before the LLM: a scraped order needs an include word (title or text); an exclude word in the
    # title drops it unless the title also has an include word.
    # Words match at a word start, so "бот" matches "бота" but not "работа". Empty include list disables it.
    scraper_include_keywords: str = "бот,telegram,телеграм,тг,python,питон,парс,скрипт,crm,амо,amocrm,битрикс,bitrix,интеграц,api,апи,webhook,вебхук,gpt,chatgpt,openai,yandexgpt,llm,нейросет,ии,ai,ai-,автоматизац,автоматиз,mini app,миниапп,fastapi,django,aiogram,лендинг,landing,rag,langchain,crewai,pinecone,dify,мультиагент,база знаний,базы знаний,квалификация лидов,квалификации лидов,автоматизация продаж,чат-бот,docker,kubernetes,k8s,devops,ci/cd,cicd,gitlab,github actions,деплой,задеплоить,развернуть,развертыван,развёртыван,сервер,vps,vds,хостинг,nginx,linux,линукс,администрирован,мониторинг,grafana,prometheus,terraform,ansible,бэкап,резервн,ssl,ollama,vllm,gpu,облак,cloud"
    scraper_exclude_keywords: str = "логотип,дизайн,баннер,видео,монтаж,рилс,reels,прозвон,обзвон,звонк,копирайт,рерайт,статьи,статья,статей,smm,таргет,директ,seo,продвижен,перевод,озвуч,фотограф,иллюстрац,отзыв,1с,1c,unity,wordpress,figma"
    telegram_channels: str = ""
    brightdata_proxy_url: str = ""
    brightdata_token: str = ""
    brightdata_zone: str = ""
    brightdata_sources: str = "kwork"
    brightdata_ca_cert: str = ""
    manual_payment_mode: bool = True
    prodamus_secret_key: str = ""
    yookassa_shop_id: str = ""
    yookassa_secret_key: str = ""
    reports_dir: str = "./reports"

    @field_validator("database_url")
    @classmethod
    def sync_driver(cls, value: str) -> str:
        # The application uses synchronous SQLAlchemy sessions; accept async URLs from deploy templates.
        return value.replace("postgresql+asyncpg://", "postgresql+psycopg://")

    def allowed_ids(self) -> set[int]:
        return {int(x) for x in self.telegram_allowed_user_ids.split(",") if x.strip().isdigit()}

    def webhook_secret(self) -> str:
        if self.telegram_webhook_secret:
            return self.telegram_webhook_secret
        return hashlib.sha256(f"telegram-webhook:{self.telegram_bot_token}".encode()).hexdigest()


@lru_cache
def settings() -> Settings:
    return Settings()
