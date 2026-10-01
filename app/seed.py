from sqlalchemy import select

from app.db import SessionLocal
from app.models import AgencyProfile, Experiment, KnowledgeItem, Service
from app.schemas import LeadInput
from app.services import create_lead

# Current stack; `freelance seed` rewrites the profile's skills with it on every run.
SKILLS = ["Python", "Telegram API", "aiogram", "n8n", "make.com", "amoCRM", "Bitrix24", "LLM integrations", "FastAPI", "Docker"]
# Work the agency does not take; the analysis prompt rejects such orders.
LIMITATIONS = ["Не делаем: дизайн и графику без разработки (логотипы, баннеры, макеты в Figma)", "Не делаем: видео, монтаж, фото, озвучку", "Не делаем: обзвоны, продажи, работу с клиентами", "Не делаем: тексты, SEO, SMM, контекстную и таргетированную рекламу", "Не делаем: 1С, игры (Unity/Lua), мобильные нативные приложения", "Не делаем: работу без программирования (ввод данных, отзывы, консультации)"]


def seed(demo: bool = False):
    with SessionLocal() as db:
        profile = db.scalar(select(AgencyProfile).order_by(AgencyProfile.id).limit(1))
        if profile is None:
            db.add(AgencyProfile(name="Small technology agency", description="AI automation, Telegram bots and landing pages", positioning="Practical software for small businesses", skills=SKILLS, technologies=["Python", "FastAPI", "aiogram", "n8n", "Docker", "PostgreSQL"], industries=[], advantages=[], limitations=LIMITATIONS))
        else:
            profile.skills = SKILLS
            profile.limitations = profile.limitations or LIMITATIONS
        catalog = [("AI_AUTOMATION", "AI automation", 600, 300, 5000, 12), ("TELEGRAM_BOT", "Telegram bot", 450, 250, 4000, 10), ("LANDING_PAGE", "Landing page", 350, 200, 3000, 8)]
        for category, name, base, minimum, maximum, hours in catalog:
            if not db.scalar(select(Service).where(Service.category == category)):
                db.add(Service(category=category, name=name, description=name, base_price=base, min_price=minimum, max_price=maximum, typical_hours=hours, required_skills=[], margin_target=.3))
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
