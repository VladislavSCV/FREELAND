import asyncio
import csv
import io
import json
import logging
import secrets
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import (
    AgencyProfile,
    Application,
    CaseStudy,
    Client,
    Experiment,
    FollowUp,
    KnowledgeItem,
    Lead,
    LeadAnalysis,
    Payment,
    Project,
    Proposal,
    Service,
    now,
)
from app.schemas import (
    AgencyProfileInput,
    CaseStudyInput,
    ExperimentInput,
    KnowledgeInput,
    LeadInput,
    ProjectInput,
    ProjectUpdate,
    ProposalUpdate,
    ServiceInput,
    ServiceUpdate,
)
from app.services import (
    analyze,
    approve,
    audit,
    close_project,
    create_lead,
    generate_proposal,
    mark_sent,
    transition,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
_background: set[asyncio.Task] = set()


@asynccontextmanager
async def lifespan(_: FastAPI):
    cfg = settings()
    bot = None
    if cfg.telegram_bot_token and cfg.telegram_mode == "webhook" and cfg.webhook_domain:
        from app.bot import dp, get_bot
        bot = get_bot()
        try:
            await bot.set_webhook(f"{cfg.webhook_domain.rstrip('/')}/api/v1/telegram/webhook", secret_token=cfg.webhook_secret(), allowed_updates=dp.resolve_used_update_types())
            log.info("Telegram webhook registered at %s", cfg.webhook_domain)
        except Exception:
            log.exception("Telegram webhook registration failed; updates will not arrive until it succeeds")
    yield
    if bot is not None:
        await bot.session.close()


app = FastAPI(title=settings().project_name, version="0.2.0", lifespan=lifespan)
_rate_buckets: dict[str, list[float]] = {}
_redis = Redis.from_url(settings().redis_url)


@app.middleware("http")
async def guard_requests(request: Request, call_next):
    if request.url.path.startswith("/api/"):
        length = request.headers.get("content-length")
        if length and (not length.isdigit() or int(length) > 2_100_000):
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "Payload too large"}, status_code=413)
        key = f"rate:{request.client.host if request.client else 'unknown'}:{int(time.time() // 60)}"
        try:
            async with _redis.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, 70)
                count, _ = await pipe.execute()
        except RedisError:
            current = time.monotonic()
            bucket = [value for value in _rate_buckets.get(key, []) if current - value < 60]
            bucket.append(current)
            _rate_buckets[key] = bucket
            count = len(bucket)
        if count > 120:
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "Rate limit exceeded"}, status_code=429)
    return await call_next(request)


def auth(authorization: str | None = Header(default=None)):
    token = settings().api_token
    if not token or not authorization or not secrets.compare_digest(authorization, f"Bearer {token}"):
        raise HTTPException(401, "Invalid API token")


DONE = ("COMPLETED", "PAID")
DB = Annotated[Session, Depends(get_db)]
Auth = Annotated[None, Depends(auth)]


def obj(row):
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def get_lead(db: Session, lead_id: int) -> Lead:
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    return lead


@app.get("/health")
def health(db: DB):
    db.scalar(select(func.count()).select_from(Lead))
    return {"status": "ok"}


@app.post("/api/v1/leads")
def add_lead(item: LeadInput, db: DB, _: Auth):
    lead, created = create_lead(db, item)
    return {"lead": obj(lead), "created": created}


@app.get("/api/v1/leads")
def list_leads(db: DB, _: Auth, status: str | None = None, limit: int = 50):
    query = select(Lead).order_by(Lead.id.desc()).limit(min(max(limit, 1), 200))
    if status:
        query = query.where(Lead.status == status)
    return [obj(x) for x in db.scalars(query)]


@app.get("/api/v1/leads/{lead_id}")
def lead_detail(lead_id: int, db: DB, _: Auth):
    lead = get_lead(db, lead_id)
    analysis = db.scalar(select(LeadAnalysis).where(LeadAnalysis.lead_id == lead_id))
    proposals = db.scalars(select(Proposal).where(Proposal.lead_id == lead_id).order_by(Proposal.id.desc())).all()
    return {"lead": obj(lead), "analysis": obj(analysis) if analysis else None, "proposals": [obj(p) for p in proposals]}


@app.post("/api/v1/leads/{lead_id}/analyze")
async def analyze_lead(lead_id: int, db: DB, _: Auth):
    return obj(await analyze(db, get_lead(db, lead_id)))


@app.post("/api/v1/leads/{lead_id}/proposal")
async def make_proposal(lead_id: int, db: DB, _: Auth):
    return obj(await generate_proposal(db, get_lead(db, lead_id)))


@app.post("/api/v1/leads/{lead_id}/approve")
def approve_proposal(lead_id: int, db: DB, _: Auth):
    return obj(approve(db, get_lead(db, lead_id)))


@app.post("/api/v1/leads/{lead_id}/mark-sent")
def sent_proposal(lead_id: int, db: DB, _: Auth):
    return obj(mark_sent(db, get_lead(db, lead_id)))


@app.post("/api/v1/leads/{lead_id}/outcome/{outcome}")
def outcome(lead_id: int, outcome: str, db: DB, _: Auth):
    if outcome not in {"REPLIED", "WON", "LOST", "REJECTED", "ARCHIVED"}:
        raise HTTPException(422, "Invalid outcome")
    lead = get_lead(db, lead_id)
    transition(lead, outcome)
    audit(db, "lead", lead.id, outcome.lower())
    db.commit()
    return obj(lead)


@app.get("/api/v1/proposals")
def proposals(db: DB, _: Auth, status: str | None = None):
    query = select(Proposal).order_by(Proposal.id.desc()).limit(200)
    if status:
        query = query.where(Proposal.status == status)
    return [obj(x) for x in db.scalars(query)]


@app.patch("/api/v1/proposals/{proposal_id}")
def edit_proposal(proposal_id: int, item: ProposalUpdate, db: DB, _: Auth):
    proposal = db.get(Proposal, proposal_id)
    if not proposal:
        raise HTTPException(404, "Proposal not found")
    if proposal.status != "DRAFT":
        raise HTTPException(409, "Only drafts can be edited")
    changes = item.model_dump(exclude_unset=True)
    for key, value in changes.items():
        if value is not None:
            setattr(proposal, key, value)
    audit(db, "proposal", proposal.id, "edited", {"fields": list(changes)})
    db.commit()
    return obj(proposal)


@app.get("/api/v1/clients")
def clients(db: DB, _: Auth):
    return [obj(x) for x in db.scalars(select(Client).order_by(Client.id.desc()).limit(200))]


@app.get("/api/v1/projects")
def projects(db: DB, _: Auth):
    return [obj(x) for x in db.scalars(select(Project).order_by(Project.id.desc()).limit(200))]


@app.post("/api/v1/projects")
def create_project(item: ProjectInput, db: DB, _: Auth):
    lead = get_lead(db, item.lead_id)
    if lead.status != "WON":
        raise HTTPException(409, "Lead must be won")
    existing = db.scalar(select(Project).where(Project.lead_id == lead.id))
    if existing:
        return obj(existing)
    analysis = db.scalar(select(LeadAnalysis).where(LeadAnalysis.lead_id == lead.id))
    proposal = db.scalar(select(Proposal).where(Proposal.lead_id == lead.id).order_by(Proposal.id.desc()))
    client = Client(name=lead.client_name or "Unknown client", country=lead.client_country, source=lead.source, profile_url=lead.url, rating=lead.client_rating, jobs_count=lead.client_jobs_count)
    db.add(client)
    db.flush()
    project = Project(lead_id=lead.id, client_id=client.id, name=lead.title, service_category=analysis.service_category if analysis else "OTHER", scope=item.scope or lead.description, notes=item.notes, contract_price=proposal.price if proposal else 0, currency=lead.currency, estimated_hours=analysis.estimated_hours if analysis else 0, deadline=item.deadline)
    db.add(project)
    db.flush()
    audit(db, "project", project.id, "created")
    db.commit()
    return obj(project)


@app.patch("/api/v1/projects/{project_id}")
def update_project(project_id: int, item: ProjectUpdate, db: DB, _: Auth):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    if project.status in {"COMPLETED", "CANCELLED", "PAID"}:
        raise HTTPException(409, "Project is closed")
    changes = item.model_dump(exclude_unset=True)
    feedback = changes.pop("client_feedback", None)
    for key, value in changes.items():
        if value is not None:
            setattr(project, key, value)
    if project.status == "COMPLETED":
        close_project(project, feedback)
    audit(db, "project", project.id, "updated", changes)
    db.commit()
    return obj(project)


@app.get("/api/v1/analytics/dashboard")
def dashboard(db: DB, _: Auth):
    leads = db.scalars(select(Lead)).all()
    proposals = db.scalars(select(Proposal)).all()
    projects = db.scalars(select(Project)).all()
    today = now().date()
    sent = [p for p in proposals if p.status == "SENT"]
    won = [l for l in leads if l.status == "WON"]
    return {"lead_count": len(leads), "new_today": sum(l.created_at.date() == today for l in leads), "qualified": sum(l.status == "QUALIFIED" for l in leads), "proposal_count": len(proposals), "proposals_ready": sum(p.status == "DRAFT" for p in proposals), "sent": len(sent), "replies": sum(l.status == "REPLIED" for l in leads), "won": len(won), "revenue": sum(p.contract_price for p in projects if p.status in DONE), "profit": sum(p.profit for p in projects if p.status in DONE), "hours": sum(p.actual_hours for p in projects if p.status in DONE), "paid": sum(p.paid_amount or 0 for p in projects), "pipeline": sum(p.price for p in proposals if p.status in {"DRAFT", "APPROVED", "SENT"})}


@app.get("/api/v1/analytics/conversion")
def conversion(db: DB, _: Auth):
    leads = db.scalars(select(Lead)).all()
    sent = sum(l.status in {"SENT", "REPLIED", "WON", "LOST"} for l in leads)
    replied = sum(l.status in {"REPLIED", "WON", "LOST"} for l in leads)
    won = sum(l.status == "WON" for l in leads)
    return {"lead_count": len(leads), "sent_count": sent, "reply_rate": replied / sent if sent else 0, "conversion_rate": won / len(leads) if leads else 0, "win_rate": won / sent if sent else 0}


@app.get("/api/v1/analytics/revenue")
def revenue(db: DB, _: Auth):
    projects = db.scalars(select(Project).where(Project.status.in_(DONE))).all()
    by_month = defaultdict(lambda: {"revenue": 0.0, "profit": 0.0, "hours": 0.0})
    for p in projects:
        bucket = by_month[p.completed_at.strftime("%Y-%m")]
        bucket["revenue"] += p.contract_price
        bucket["profit"] += p.profit
        bucket["hours"] += p.actual_hours
    return {"by_month": dict(by_month), "average_project_value": sum(p.contract_price for p in projects) / len(projects) if projects else 0, "effective_hourly_rate": sum(p.profit for p in projects) / sum(p.actual_hours for p in projects) if sum(p.actual_hours for p in projects) else 0}


@app.get("/api/v1/analytics/breakdown")
def breakdown(db: DB, _: Auth):
    leads = db.scalars(select(Lead)).all()
    analyses = {a.lead_id: a for a in db.scalars(select(LeadAnalysis))}
    groups = {dimension: defaultdict(lambda: {"leads": 0, "won": 0, "replied": 0}) for dimension in ("source", "service", "price_range", "score_range", "month")}
    for lead in leads:
        analysis = analyses.get(lead.id)
        price = analysis.recommended_price if analysis else 0
        score_value = analysis.overall_score if analysis else 0
        values = {"source": lead.source, "service": analysis.service_category if analysis else "UNKNOWN", "price_range": "<500" if price < 500 else "500-999" if price < 1000 else "1000+", "score_range": "<50" if score_value < 50 else "50-69" if score_value < 70 else "70+", "month": lead.created_at.strftime("%Y-%m")}
        for dimension, value in values.items():
            bucket = groups[dimension][value]
            bucket["leads"] += 1
            bucket["won"] += lead.status == "WON"
            bucket["replied"] += lead.status in {"REPLIED", "WON", "LOST"}
    return {dimension: dict(values) for dimension, values in groups.items()}


@app.get("/api/v1/agency")
def agency(db: DB, _: Auth):
    profile = db.scalar(select(AgencyProfile).limit(1))
    return obj(profile) if profile else None


@app.put("/api/v1/agency")
def update_agency(item: AgencyProfileInput, db: DB, _: Auth):
    profile = db.scalar(select(AgencyProfile).limit(1)) or AgencyProfile()
    for key, value in item.model_dump().items():
        setattr(profile, key, value)
    db.add(profile)
    db.commit()
    return obj(profile)


@app.get("/api/v1/case-studies")
def case_studies(db: DB, _: Auth):
    return [obj(x) for x in db.scalars(select(CaseStudy).order_by(CaseStudy.id.desc()).limit(200))]


@app.post("/api/v1/case-studies")
def add_case_study(item: CaseStudyInput, db: DB, _: Auth):
    row = CaseStudy(**item.model_dump())
    db.add(row)
    db.commit()
    return obj(row)


@app.post("/api/v1/knowledge")
def add_knowledge(item: KnowledgeInput, db: DB, _: Auth):
    row = KnowledgeItem(**item.model_dump())
    db.add(row)
    db.commit()
    return obj(row)


@app.get("/api/v1/knowledge")
def knowledge(db: DB, _: Auth, category: str | None = None):
    query = select(KnowledgeItem).order_by(KnowledgeItem.id.desc()).limit(200)
    if category:
        query = query.where(KnowledgeItem.category == category)
    return [obj(x) for x in db.scalars(query)]


@app.post("/api/v1/experiments")
def add_experiment(item: ExperimentInput, db: DB, _: Auth):
    row = Experiment(**item.model_dump())
    db.add(row)
    db.commit()
    return obj(row)


@app.get("/api/v1/experiments")
def experiments(db: DB, _: Auth):
    return [obj(x) for x in db.scalars(select(Experiment).order_by(Experiment.id.desc()).limit(200))]


@app.get("/api/v1/services")
def services(db: DB, _: Auth):
    return [obj(x) for x in db.scalars(select(Service))]


@app.post("/api/v1/services")
def add_service(item: ServiceInput, db: DB, _: Auth):
    if not item.min_price <= item.base_price <= item.max_price:
        raise HTTPException(422, "Invalid price range")
    row = Service(**item.model_dump())
    db.add(row)
    db.commit()
    return obj(row)


@app.patch("/api/v1/services/{category}")
def update_service(category: str, item: ServiceUpdate, db: DB, _: Auth):
    service = db.scalar(select(Service).where(Service.category == category))
    if not service:
        raise HTTPException(404, "Service not found")
    for key, value in item.model_dump(exclude_unset=True).items():
        setattr(service, key, value or None if key == "demo_url" else value)
    if not service.min_price <= service.base_price <= service.max_price:
        raise HTTPException(422, "Invalid price range")
    db.commit()
    return obj(service)


@app.post("/api/v1/ingestion/webhook/{source}")
def webhook(source: str, item: LeadInput, db: DB, x_webhook_token: str | None = Header(default=None)):
    if not settings().webhook_token or not x_webhook_token or not secrets.compare_digest(x_webhook_token, settings().webhook_token):
        raise HTTPException(401, "Invalid webhook token")
    item.source = source[:80]
    lead, created = create_lead(db, item)
    return {"lead": obj(lead), "created": created}


@app.post("/api/v1/ingestion/file")
async def import_file(db: DB, _: Auth, file: Annotated[UploadFile, File()]):
    data = await file.read(2_000_001)
    if len(data) > 2_000_000:
        raise HTTPException(413, "File too large")
    try:
        content = data.decode("utf-8-sig")
        if file.filename and file.filename.lower().endswith(".csv"):
            rows = list(csv.DictReader(io.StringIO(content)))
        elif file.filename and file.filename.lower().endswith(".json"):
            rows = json.loads(content)
        else:
            raise HTTPException(422, "CSV or JSON required")
        if not isinstance(rows, list) or len(rows) > 1000:
            raise HTTPException(422, "Expected a list of at most 1000 leads")
        items = [LeadInput.model_validate(row) for row in rows]
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise HTTPException(422, str(error)) from error
    created = 0
    for item in items:
        item.source = "file"
        _, fresh = create_lead(db, item)
        created += fresh
    return {"received": len(items), "created": created}


@app.get("/api/v1/follow-ups")
def followups(db: DB, _: Auth):
    return [obj(x) for x in db.scalars(select(FollowUp).where(FollowUp.status == "PENDING").order_by(FollowUp.due_at))]


@app.post("/api/v1/follow-ups/{followup_id}/ignore")
def ignore_followup(followup_id: int, db: DB, _: Auth):
    item = db.get(FollowUp, followup_id)
    if not item:
        raise HTTPException(404, "Follow-up not found")
    item.status = "IGNORED"
    db.commit()
    return obj(item)


@app.get("/api/v1/applications")
def applications(db: DB, _: Auth, status: str | None = None):
    query = select(Application).order_by(Application.id.desc()).limit(200)
    if status:
        query = query.where(Application.status == status)
    return [obj(x) for x in db.scalars(query)]


@app.get("/api/v1/autopilot")
def autopilot_state(db: DB, _: Auth):
    from app.services.ai_evaluator import autopilot_enabled, daily_status
    return {"enabled": autopilot_enabled(db), "auto_apply_score": settings().auto_apply_score, "review_score": settings().review_score, "today": daily_status(db)}


@app.post("/api/v1/autopilot/{state}")
def autopilot_toggle(state: str, db: DB, _: Auth):
    from app.services.ai_evaluator import autopilot_enabled, set_autopilot
    if state not in {"on", "off"}:
        raise HTTPException(422, "Use on or off")
    set_autopilot(db, state == "on")
    return {"enabled": autopilot_enabled(db)}


def _telegram_user_id(update: dict) -> int | None:
    for key in ("message", "edited_message", "callback_query", "inline_query", "my_chat_member"):
        user = (update.get(key) or {}).get("from")
        if user:
            return user.get("id")
    return None


@app.post("/api/v1/telegram/webhook")
async def telegram_webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    cfg = settings()
    if not cfg.telegram_bot_token:
        raise HTTPException(404, "Telegram bot is not configured")
    if not x_telegram_bot_api_secret_token or not secrets.compare_digest(x_telegram_bot_api_secret_token, cfg.webhook_secret()):
        raise HTTPException(403, "Invalid Telegram secret token")
    try:
        payload = await request.json()
    except ValueError as error:
        raise HTTPException(422, "Invalid JSON") from error
    if not isinstance(payload, dict):
        raise HTTPException(422, "Invalid update")
    # Updates from users outside ALLOWED_TELEGRAM_IDS are acknowledged and dropped.
    if _telegram_user_id(payload) not in cfg.allowed_ids():
        return {"ok": True}
    from aiogram.types import Update

    from app.bot import dp, get_bot
    bot = get_bot()
    update = Update.model_validate(payload, context={"bot": bot})
    # Answer Telegram immediately; LLM-backed actions may take longer than its webhook timeout.
    task = asyncio.create_task(dp.feed_update(bot, update))
    _background.add(task)
    task.add_done_callback(_background.discard)
    return {"ok": True}


@app.post("/api/v1/webhooks/prodamus")
async def prodamus_webhook(request: Request, db: DB, sign: str | None = Header(default=None)):
    from app.services.payments import handle_prodamus
    form = await request.form()
    payment = handle_prodamus(db, {k: str(v) for k, v in form.multi_items()}, sign)
    return {"ok": True, "payment": obj(payment) if payment else None}


@app.post("/api/v1/webhooks/yookassa")
async def yookassa_webhook(request: Request, db: DB):
    from app.services.payments import handle_yookassa
    try:
        body = await request.json()
    except ValueError as error:
        raise HTTPException(422, "Invalid JSON") from error
    if not isinstance(body, dict):
        raise HTTPException(422, "Invalid notification")
    payment = await handle_yookassa(db, body)
    return {"ok": True, "payment": obj(payment) if payment else None}


@app.post("/api/v1/projects/{project_id}/payments")
def add_payment(project_id: int, amount: float, db: DB, _: Auth):
    from app.services.payments import manual_payment
    return obj(manual_payment(db, project_id, amount))


@app.get("/api/v1/payments")
def payments(db: DB, _: Auth):
    return [obj(x) for x in db.scalars(select(Payment).order_by(Payment.id.desc()).limit(200))]
