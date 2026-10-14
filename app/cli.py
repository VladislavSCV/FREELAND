import asyncio
import json
from pathlib import Path

import typer

from app.db import SessionLocal
from app.main import dashboard, health
from app.models import KnowledgeItem, Lead
from app.schemas import LeadInput
from app.seed import seed
from app.services import analyze, create_lead, generate_proposal

cli = typer.Typer()
lead_cli = typer.Typer()
proposal_cli = typer.Typer()
knowledge_cli = typer.Typer()
cli.add_typer(lead_cli, name="lead")
cli.add_typer(proposal_cli, name="proposal")
cli.add_typer(knowledge_cli, name="knowledge")


@cli.command()
def healthcheck():
    with SessionLocal() as db:
        typer.echo(health(db))


@cli.command("health")
def health_command():
    healthcheck()


@cli.command("seed")
def seed_command(demo: bool = typer.Option(False, help="Also create demo leads")):
    seed(demo)
    typer.echo("Seed complete")


@cli.command("stats")
def stats():
    with SessionLocal() as db:
        typer.echo(json.dumps(dashboard(db, None), indent=2))


@cli.command("cleanup-demo")
def cleanup_demo_command():
    """Delete smoke/demo leads, their projects and proposals from the database and analytics."""
    from app.services.maintenance import cleanup_demo
    with SessionLocal() as db:
        typer.echo(json.dumps(cleanup_demo(db)))


@cli.command("archive-pending")
def archive_pending_command():
    """Archive every application waiting for review (stale Telegram cards)."""
    from app.services.maintenance import archive_pending
    with SessionLocal() as db:
        typer.echo(f"Archived {archive_pending(db)} applications")


@cli.command("reevaluate")
def reevaluate_command(all_archived: bool = typer.Option(False, "--all", help="Ignore the keyword filter")):
    """Re-run the autopilot on archived leads that pass the scraper keyword filter."""
    from app.config import settings
    from app.schemas import LeadInput
    from app.services.maintenance import reevaluate_archived
    from app.workers.scraper import KeywordFilter
    keywords = KeywordFilter(settings().scraper_include_keywords, settings().scraper_exclude_keywords)

    def accepts(lead) -> bool:
        return all_archived or keywords.accepts(LeadInput(source=lead.source, title=lead.title, description=lead.description))

    with SessionLocal() as db:
        typer.echo(f"Requeued {reevaluate_archived(db, accepts)} leads; the worker evaluates them within a few minutes")


@cli.command("channels-check")
def channels_check(channels: list[str]):
    """Check public Telegram channels: alive, posting project orders (not vacancies), passing the filter."""
    from app.config import settings
    from app.workers.channels import check_channels
    from app.workers.scraper import Fetcher, KeywordFilter

    cfg = settings()
    keywords = KeywordFilter(cfg.scraper_include_keywords, cfg.scraper_exclude_keywords)

    async def run():
        fetcher = Fetcher(cfg)
        try:
            return await check_channels([c for arg in channels for c in arg.replace(",", " ").split()], fetcher,
                                        keywords, cfg.telegram_api_url or "https://t.me")
        finally:
            await fetcher.aclose()

    typer.echo("канал | вердикт | постов за 7 дн | заказов | вакансий | подходящих заказов | последний пост")
    for r in asyncio.run(run()):
        last = f"{r.last_post:%Y-%m-%d}" if r.last_post else "-"
        typer.echo(f"{r.channel} | {r.verdict} | {r.week_posts} | {r.week_orders} | {r.week_vacancies} | {r.week_relevant_orders} | {last} | {' / '.join(r.examples)}")


@cli.command("tg-login")
def tg_login():
    """One-time login to your Telegram account for one-click proposals; prints TG_USER_SESSION.

    Run interactively: docker compose run --rm api freelance tg-login"""
    from telethon.sessions import StringSession
    from telethon.sync import TelegramClient

    from app.config import settings
    from app.services.outreach import proxy_options

    cfg = settings()
    if not cfg.tg_user_api_id or not cfg.tg_user_api_hash:
        raise typer.BadParameter("Set TG_USER_API_ID and TG_USER_API_HASH (my.telegram.org -> API development tools)")
    with TelegramClient(StringSession(), cfg.tg_user_api_id, cfg.tg_user_api_hash, **proxy_options(cfg.tg_user_proxy)) as client:
        me = client.get_me()
        typer.echo(f"\nВход выполнен: {me.first_name} (@{me.username}).")
        typer.echo("Добавьте строку в .env и никому её не показывайте — это полный доступ к аккаунту:\n")
        typer.echo(f"TG_USER_SESSION={client.session.save()}")


@cli.command("scrape")
def scrape_command():
    """Run one scraping pass over the configured sources."""
    from app.workers.scraper import scrape
    with SessionLocal() as db:
        typer.echo(json.dumps(asyncio.run(scrape(db))))


@lead_cli.command("add")
def lead_add(title: str, description: str, url: str = ""):
    with SessionLocal() as db:
        row, created = create_lead(db, LeadInput(title=title, description=description, url=url or None, source="cli"))
        typer.echo(f"Lead {row.id}; created={created}")


@lead_cli.command("analyze")
def lead_analyze(lead_id: int):
    with SessionLocal() as db:
        lead = db.get(Lead, lead_id)
        if not lead:
            raise typer.BadParameter("Lead not found")
        result = asyncio.run(analyze(db, lead))
        typer.echo(f"Score {result.overall_score}; action {result.recommended_action}")


@proposal_cli.command("generate")
def proposal_generate(lead_id: int):
    with SessionLocal() as db:
        lead = db.get(Lead, lead_id)
        if not lead:
            raise typer.BadParameter("Lead not found")
        typer.echo(asyncio.run(generate_proposal(db, lead)).body)


@knowledge_cli.command("import")
def knowledge_import(path: Path):
    if path.suffix.lower() not in {".md", ".txt", ".json"}:
        raise typer.BadParameter("Expected markdown, txt or JSON")
    content = path.read_text(encoding="utf-8")
    if len(content) > 100000:
        raise typer.BadParameter("File too large")
    with SessionLocal() as db:
        if path.suffix.lower() == ".json":
            data = json.loads(content)
            if not isinstance(data, dict):
                raise typer.BadParameter("JSON object required")
            row = KnowledgeItem(title=str(data.get("title", path.stem))[:255], content=str(data.get("content", "")), category=str(data.get("category", "general")), source=str(path), tags=data.get("tags", []))
        else:
            row = KnowledgeItem(title=path.stem, content=content, source=str(path))
        db.add(row)
        db.commit()
        typer.echo(f"Imported {row.id}")


if __name__ == "__main__":
    cli()
