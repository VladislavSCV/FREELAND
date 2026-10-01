import difflib
import hashlib
import logging
import re
from datetime import timedelta
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.llm import observed_generate
from app.models import (
    Activity,
    AgencyProfile,
    Application,
    CaseStudy,
    FollowUp,
    Lead,
    LeadAnalysis,
    Project,
    Proposal,
    Service,
    now,
)
from app.schemas import AnalysisResult, LeadInput, ProposalResult

log = logging.getLogger(__name__)


def audit(db: Session, kind: str, entity_id: int, action: str, details: dict | None = None):
    db.add(Activity(entity_type=kind, entity_id=entity_id, action=action, details=details or {}))


def normalize(value: str) -> str:
    return " ".join(re.findall(r"\w+", value.casefold()))


def content_hash(title: str, description: str) -> str:
    return hashlib.sha256(f"{normalize(title)}|{normalize(description)}".encode()).hexdigest()


def find_duplicate(db: Session, item: LeadInput) -> Lead | None:
    match = db.scalar(select(Lead).where(Lead.content_hash == content_hash(item.title, item.description)).limit(1))
    if match:
        return match
    if item.external_id:
        match = db.scalar(select(Lead).where(Lead.source == item.source, Lead.external_id == item.external_id))
        if match:
            return match
    if item.url:
        match = db.scalar(select(Lead).where(Lead.url == item.url))
        if match:
            return match
    candidates = db.scalars(select(Lead).where(Lead.source == item.source).order_by(Lead.id.desc()).limit(500)).all()
    title, description = normalize(item.title), normalize(item.description)
    for lead in candidates:
        # Platform IDs are authoritative; scraped posts often share generic titles like "Ищу разработчика".
        if not item.external_id and normalize(lead.title) == title:
            return lead
        if len(item.description) >= 80:
            matcher = difflib.SequenceMatcher(None, normalize(lead.description), description)
            if matcher.real_quick_ratio() >= .92 and matcher.quick_ratio() >= .92 and matcher.ratio() >= .92:
                return lead
    return None


def create_lead(db: Session, item: LeadInput) -> tuple[Lead, bool]:
    duplicate = find_duplicate(db, item)
    if duplicate:
        return duplicate, False
    lead = Lead(**item.model_dump(), content_hash=content_hash(item.title, item.description))
    db.add(lead)
    db.flush()
    audit(db, "lead", lead.id, "created")
    db.commit()
    return lead, True


def transition(lead: Lead, target: str):
    allowed = {
        "NEW": {"ANALYZING", "ANALYSIS_PENDING", "REJECTED", "ARCHIVED"},
        "ANALYSIS_PENDING": {"ANALYZING", "REJECTED"},
        "ANALYZING": {"ANALYZED", "ANALYSIS_PENDING"},
        "ANALYZED": {"QUALIFIED", "REJECTED", "PROPOSAL_READY", "ARCHIVED"},
        "QUALIFIED": {"PROPOSAL_READY", "REJECTED", "ARCHIVED"},
        "PROPOSAL_READY": {"PROPOSAL_APPROVED", "REJECTED"},
        "PROPOSAL_APPROVED": {"SENT", "REJECTED"},
        "SENT": {"REPLIED", "WON", "LOST"},
        "REPLIED": {"WON", "LOST"},
        "WON": {"ARCHIVED"},
        "LOST": {"ARCHIVED"},
        "REJECTED": {"ARCHIVED"},
    }
    if target not in allowed.get(lead.status, set()):
        raise HTTPException(409, f"Invalid transition {lead.status} -> {target}")
    lead.status = target


def score(result: AnalysisResult, client_known: bool = True) -> float:
    """Weighted 0-100 score. Most marketplace clients have no public history; then the client weight
    moves to fit and profit (60/40, i.e. +12%/+8% with the default 20%) instead of dragging the score down."""
    cfg = settings()
    weights = [cfg.score_fit_weight, cfg.score_profit_weight, cfg.score_client_weight, cfg.score_risk_weight]
    if not client_known:
        weights = [weights[0] + weights[2] * 0.6, weights[1] + weights[2] * 0.4, 0, weights[3]]
    if sum(weights) <= 0:
        raise ValueError("Scoring weights must sum to a positive number")
    return round((result.fit_score * weights[0] + result.profit_score * weights[1] + result.client_quality_score * weights[2] + (100 - result.risk_score) * weights[3]) / sum(weights), 2)


def convert(amount: float, currency: str) -> float | None:
    """Convert an amount from DEFAULT_CURRENCY into `currency`; None when the rate is unknown."""
    cfg = settings()
    if currency == cfg.default_currency:
        return amount
    rates = dict(pair.split("=", 1) for pair in cfg.currency_rates.replace(" ", "").split(",") if "=" in pair)
    try:
        return amount * float(rates[currency])
    except (KeyError, ValueError):
        return None


def score_10(overall: float) -> int:
    return min(10, max(1, round(overall / 10)))


def economics(hours: float, price: float, external_cost: float = 0) -> dict:
    if hours <= 0 or price < 0 or external_cost < 0:
        raise ValueError("Invalid economics input")
    profit = round(price - external_cost, 2)
    return {"estimated_hours": hours, "recommended_price": round(price, 2), "estimated_external_cost": external_cost, "estimated_profit": profit, "effective_hourly_rate": round(profit / hours, 2)}


def close_project(project: Project, feedback: str | None = None):
    """Record final economics and queue the AI postmortem for a finished project."""
    project.completed_at = project.completed_at or now()
    project.profit = round(project.contract_price - project.external_cost, 2)
    project.postmortem = {"estimated_hours": project.estimated_hours, "actual_hours": project.actual_hours, "hour_variance": round(project.actual_hours - project.estimated_hours, 2), "profit": project.profit, "effective_hourly_rate": round(project.profit / project.actual_hours, 2) if project.actual_hours else None, "client_feedback": feedback or "", "lesson": "Review estimate against actual hours" if project.actual_hours > project.estimated_hours else "Estimate was within budget", "ai_status": "PENDING"}


def prompt(db: Session, name: str) -> str:
    from app.models import PromptVersion
    row = db.scalar(select(PromptVersion).where(PromptVersion.prompt_name == name, PromptVersion.active == True).order_by(PromptVersion.version.desc()))
    if row:
        return row.content
    return (Path.cwd() / "prompts" / f"{name}.md").read_text(encoding="utf-8")


async def analyze(db: Session, lead: Lead) -> LeadAnalysis:
    if lead.status not in {"NEW", "ANALYSIS_PENDING"}:
        existing = db.scalar(select(LeadAnalysis).where(LeadAnalysis.lead_id == lead.id))
        if existing:
            return existing
        raise HTTPException(409, "Lead cannot be analyzed")
    transition(lead, "ANALYZING")
    lead.analysis_attempts = (lead.analysis_attempts or 0) + 1
    db.commit()
    try:
        services = db.scalars(select(Service)).all()
        profile = db.scalar(select(AgencyProfile).limit(1))
        currency = lead.currency

        def local(amount: float) -> float | None:
            value = convert(amount, currency)
            return round(value, 2) if value is not None else None

        context = {"lead": {"title": lead.title, "description": lead.description, "budget_min": lead.budget_min, "budget_max": lead.budget_max, "currency": currency, "client_rating": lead.client_rating, "source": lead.source}, "agency_profile": {"skills": profile.skills, "technologies": profile.technologies, "limitations": profile.limitations} if profile else {}, "services": [{"category": s.category, "description": s.description, "typical_hours": s.typical_hours, "min_price": local(s.min_price), "currency": currency} for s in services]}
        result = await observed_generate(db, "lead_analysis", prompt(db, "lead_analysis"), context, AnalysisResult)
        service = next((s for s in services if s.category == result.service_category), None)
        hours = result.estimated_hours
        # Catalog limits and the hourly rate are configured in DEFAULT_CURRENCY; skip them when no rate is known.
        floors = [result.recommended_price, local(service.min_price) if service else None, local(hours * settings().default_hourly_rate)]
        price = max(value for value in floors if value is not None)
        ceiling = local(service.max_price) if service else None
        if ceiling is not None:
            price = min(price, ceiling)
        econ = economics(hours, price)
        overall = score(result, client_known=lead.client_rating is not None or bool(lead.client_jobs_count))
        analysis = LeadAnalysis(lead_id=lead.id, service_category=result.service_category, fit_score=result.fit_score, profit_score=result.profit_score, complexity_score=result.complexity_score, risk_score=result.risk_score, client_quality_score=result.client_quality_score, overall_score=overall, estimated_hours=hours, estimated_cost=econ["estimated_external_cost"], recommended_price=price, estimated_profit=econ["estimated_profit"], effective_hourly_rate=econ["effective_hourly_rate"], recommended_action="APPLY" if result.fit_score >= settings().min_fit_score and result.profit_score >= settings().min_profit_score and overall >= 70 else "MAYBE" if overall >= 50 else "REJECT", match_reason=result.match_reason, reasoning=result.reasons, risks=result.risks, missing_information=result.missing_information, required_skills=result.required_skills)
        db.add(analysis)
        transition(lead, "ANALYZED")
        if analysis.recommended_action == "APPLY":
            transition(lead, "QUALIFIED")
        audit(db, "lead", lead.id, "analyzed", {"score": overall})
        db.commit()
        return analysis
    except Exception:
        log.exception("Analysis failed for lead %s", lead.id)
        lead.status = "ANALYSIS_PENDING"
        db.commit()
        raise


async def generate_proposal(db: Session, lead: Lead) -> Proposal:
    if lead.status not in {"ANALYZED", "QUALIFIED"}:
        raise HTTPException(409, "Lead must be analyzed first")
    analysis = db.scalar(select(LeadAnalysis).where(LeadAnalysis.lead_id == lead.id))
    profile = db.scalar(select(AgencyProfile).limit(1))
    service = db.scalar(select(Service).where(Service.category == analysis.service_category))
    cases = db.scalars(select(CaseStudy).where(CaseStudy.service == analysis.service_category).limit(3)).all()
    context = {"lead": {"title": lead.title, "description": lead.description, "client_name": lead.client_name}, "analysis": {"reasoning": analysis.reasoning, "risks": analysis.risks, "missing_information": analysis.missing_information, "recommended_price": analysis.recommended_price}, "agency_profile": {"name": profile.name, "description": profile.description, "skills": profile.skills} if profile else {}, "service": {"name": service.name, "description": service.description} if service else {}, "relevant_case_studies": [{"title": case.title, "problem": case.problem, "solution": case.solution, "result": case.result} for case in cases], "pricing_rules": {"price": analysis.recommended_price, "currency": lead.currency}}
    result = await observed_generate(db, "proposal_generation", prompt(db, "proposal_generation"), context, ProposalResult)
    proposal = Proposal(lead_id=lead.id, body=result.body, price=analysis.recommended_price, currency=lead.currency, estimated_delivery_days=result.delivery_estimate, questions=result.questions, approach=result.approach, assumptions=result.assumptions)
    db.add(proposal)
    transition(lead, "PROPOSAL_READY")
    db.flush()
    audit(db, "proposal", proposal.id, "generated")
    db.commit()
    return proposal


def approve(db: Session, lead: Lead) -> Proposal:
    if lead.status != "PROPOSAL_READY":
        raise HTTPException(409, "Proposal is not ready")
    proposal = db.scalar(select(Proposal).where(Proposal.lead_id == lead.id).order_by(Proposal.id.desc()))
    if not proposal or proposal.status != "DRAFT":
        raise HTTPException(409, "No draft proposal")
    proposal.status = "APPROVED"
    transition(lead, "PROPOSAL_APPROVED")
    _sync_application(db, lead, "PENDING_REVIEW", "APPROVED")
    audit(db, "proposal", proposal.id, "approved")
    db.commit()
    return proposal


def _sync_application(db: Session, lead: Lead, current: str, target: str):
    """Keep the autopilot record consistent when a lead moves via API or lead buttons."""
    application = db.scalar(select(Application).where(Application.lead_id == lead.id))
    if application and application.status == current:
        application.status = target
        if target == "SENT":
            application.sent_at = now()


def mark_sent(db: Session, lead: Lead) -> Proposal:
    if lead.status != "PROPOSAL_APPROVED":
        raise HTTPException(409, "Approval required")
    proposal = db.scalar(select(Proposal).where(Proposal.lead_id == lead.id).order_by(Proposal.id.desc()))
    proposal.status = "SENT"
    proposal.sent_at = now()
    transition(lead, "SENT")
    _sync_application(db, lead, "APPROVED", "SENT")
    db.add(FollowUp(lead_id=lead.id, due_at=now() + timedelta(days=settings().followup_days)))
    audit(db, "proposal", proposal.id, "marked_sent")
    db.commit()
    return proposal
