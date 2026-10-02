from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import AgencyProfile, Experiment, KnowledgeItem, Service
from app.schemas import LeadInput
from app.services import create_lead

# Current stack; `freelance seed` rewrites the profile's skills with it on every run.
# Development / automation first, then DevOps / SRE / platform and AI infrastructure.
SKILLS = [
    "Python", "Telegram API", "aiogram", "amoCRM", "Bitrix24", "LLM integrations", "FastAPI", "Docker",
    "Linux", "Docker Compose", "Kubernetes", "CI/CD (GitLab CI, GitHub Actions)", "Terraform", "Ansible",
    "Nginx", "Prometheus", "Grafana", "PostgreSQL", "Ollama", "vLLM", "RAG",
    "Сети и VPN (WireGuard)", "Хостинг и панели (Beget, Reg.ru, Timeweb)",
]
TECHNOLOGIES = ["Python", "FastAPI", "aiogram", "Docker", "PostgreSQL", "Kubernetes", "Terraform", "Prometheus", "Grafana"]
# Work the agency does not take; the analysis prompt rejects such orders.
LIMITATIONS = ["Не делаем: дизайн и графику без разработки (логотипы, баннеры, макеты в Figma)", "Не делаем: видео, монтаж, фото, озвучку", "Не делаем: обзвоны, продажи, работу с клиентами", "Не делаем: тексты, SEO, SMM, контекстную и таргетированную рекламу", "Не делаем: 1С, игры (Unity/Lua), мобильные нативные приложения", "Не делаем: работу без программирования (ввод данных, отзывы, консультации)", "Не делаем: no-code-сценарии в n8n, Make, Zapier (интеграции пишем кодом на Python)"]


# Prices are in DEFAULT_CURRENCY. Ruble defaults fit FL.ru / Kwork budgets.
# The LLM picks the category by these descriptions, so they say concretely what each service covers.
DESCRIPTIONS = {
    "AI_AUTOMATION": "Автоматизация бизнес-процессов кодом: интеграции CRM (amoCRM, Битрикс24) с сервисами и API, парсеры, скрипты, AI-агенты и обработка данных через LLM",
    "TELEGRAM_BOT": "Telegram-боты и mini apps на Python/aiogram: запись, приём заявок, оплаты, уведомления, боты с LLM",
    "LANDING_PAGE": "Лендинги и небольшие сайты с вёрсткой и формами заявок",
    "DEVOPS_INFRA": "DevOps и администрирование: настройка VPS и Linux-серверов, Docker и Compose, CI/CD, деплой и перенос проекта на хостинг, Nginx и SSL, мониторинг (Prometheus, Grafana), бэкапы, Kubernetes, Terraform, Ansible",
    "AI_INFRA": "AI-инфраструктура: развёртывание LLM на своём сервере (Ollama, vLLM), RAG с векторной базой, GPU-серверы, API и мониторинг для моделей",
}
CATALOG_RUB = [("AI_AUTOMATION", "AI automation", 30000, 12000, 400000, 12), ("TELEGRAM_BOT", "Telegram bot", 25000, 10000, 300000, 10), ("LANDING_PAGE", "Landing page", 15000, 7000, 200000, 8), ("DEVOPS_INFRA", "DevOps / infrastructure", 15000, 5000, 150000, 6), ("AI_INFRA", "AI infrastructure", 50000, 20000, 300000, 16)]
CATALOG_USD = [("AI_AUTOMATION", "AI automation", 600, 300, 5000, 12), ("TELEGRAM_BOT", "Telegram bot", 450, 250, 4000, 10), ("LANDING_PAGE", "Landing page", 350, 200, 3000, 8), ("DEVOPS_INFRA", "DevOps / infrastructure", 200, 60, 2000, 6), ("AI_INFRA", "AI infrastructure", 600, 250, 4000, 16)]
OLD_USD_PRICES = {(base, minimum, maximum) for _, _, base, minimum, maximum, _ in CATALOG_USD}


def seed(demo: bool = False):
    with SessionLocal() as db:
        profile = db.scalar(select(AgencyProfile).order_by(AgencyProfile.id).limit(1))
        if profile is None:
            db.add(AgencyProfile(name="Small technology agency", description="AI automation, Telegram bots and landing pages", positioning="Practical software for small businesses", skills=SKILLS, technologies=TECHNOLOGIES, industries=[], advantages=[], limitations=LIMITATIONS))
        else:
            profile.skills = SKILLS
            profile.technologies = TECHNOLOGIES
            missing = [item for item in LIMITATIONS if item not in (profile.limitations or [])]
            profile.limitations = [*(profile.limitations or []), *missing]
        catalog = CATALOG_RUB if settings().default_currency == "RUB" else CATALOG_USD
        for category, name, base, minimum, maximum, hours in catalog:
            service = db.scalar(select(Service).where(Service.category == category))
            if service is None:
                db.add(Service(category=category, name=name, description=DESCRIPTIONS[category], base_price=base, min_price=minimum, max_price=maximum, typical_hours=hours, required_skills=[], margin_target=.3))
                continue
            if catalog is CATALOG_RUB and (service.base_price, service.min_price, service.max_price) in OLD_USD_PRICES:
                # Untouched USD defaults from an older seed: switch them to the ruble catalog.
                service.base_price, service.min_price, service.max_price = base, minimum, maximum
            if not service.description or service.description == service.name:
                # Older seeds stored the name as description; keep descriptions edited by hand.
                service.description = DESCRIPTIONS[category]
        if not db.scalar(select(Experiment).limit(1)):
            db.add(Experiment(name="Proposal length", hypothesis="Shorter proposals may improve reply rate", variable="proposal_length", old_value="long", new_value="short"))
        if not db.scalar(select(KnowledgeItem).limit(1)):
            db.add(KnowledgeItem(title="Qualification checklist", content="Confirm scope, deadline, budget, acceptance criteria and ownership of external accounts.", category="process", source="seed"))
        db.commit()
        if not demo:
            # Real orders come from the scraper worker; demo leads only on request.
            return
        for title, description in [("Telegram support bot", "Need a Telegram bot to answer common support questions"), ("AI document automation", "Automate document classification and extraction"), ("Product landing page", "Create a landing page for a new product")]:
            create_lead(db, LeadInput(title=title, description=description, source="seed"))


if __name__ == "__main__":
    seed()
