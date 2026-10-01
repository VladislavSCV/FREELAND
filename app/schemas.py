from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class LeadInput(BaseModel):
    external_id: str | None = Field(default=None, max_length=255)
    source: str = Field(default="manual", min_length=1, max_length=80)
    url: str | None = Field(default=None, max_length=2000)
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(min_length=1, max_length=20000)
    client_name: str | None = Field(default=None, max_length=255)
    client_country: str | None = Field(default=None, max_length=100)
    client_rating: float | None = Field(default=None, ge=0, le=5)
    client_jobs_count: int | None = Field(default=None, ge=0)
    budget_min: float | None = Field(default=None, ge=0)
    budget_max: float | None = Field(default=None, ge=0)
    currency: str = Field(default="USD", pattern="^[A-Z]{3}$")
    published_at: datetime | None = None
    raw_payload: dict = Field(default_factory=dict)


class AnalysisResult(BaseModel):
    service_category: str
    fit_score: int = Field(ge=0, le=100)
    profit_score: int = Field(ge=0, le=100)
    complexity_score: int = Field(ge=0, le=100)
    risk_score: int = Field(ge=0, le=100)
    client_quality_score: int = Field(ge=0, le=100)
    estimated_hours: float = Field(gt=0)
    recommended_price: float = Field(ge=0)
    currency: str
    recommended_action: Literal["APPLY", "MAYBE", "REJECT"]
    match_reason: str = Field(default="", max_length=1000)
    reasons: list[str]
    risks: list[str]
    missing_information: list[str]
    required_skills: list[str]


class ProposalResult(BaseModel):
    body: str = Field(min_length=20, max_length=5000)
    questions: list[str]
    assumptions: list[str]
    approach: str
    suggested_price: float = Field(ge=0)
    delivery_estimate: int = Field(ge=1)


class ProposalUpdate(BaseModel):
    body: str | None = Field(default=None, min_length=20, max_length=5000)
    price: float | None = Field(default=None, ge=0)
    estimated_delivery_days: int | None = Field(default=None, ge=1)
    questions: list[str] | None = None
    assumptions: list[str] | None = None
    approach: str | None = None


class PostmortemResult(BaseModel):
    went_well: list[str]
    went_wrong: list[str]
    underestimated_areas: list[str]
    overestimated_areas: list[str]
    recommended_price_next_time: float = Field(ge=0)
    process_change: str


class ProjectInput(BaseModel):
    lead_id: int
    deadline: datetime | None = None
    scope: str = ""
    notes: str = ""


class ProjectUpdate(BaseModel):
    actual_hours: float | None = Field(default=None, ge=0)
    external_cost: float | None = Field(default=None, ge=0)
    status: Literal["PLANNED", "IN_PROGRESS", "BLOCKED", "COMPLETED", "CANCELLED"] | None = None
    notes: str | None = None
    client_feedback: str | None = None


class KnowledgeInput(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1, max_length=100000)
    category: str = "general"
    source: str = "manual"
    tags: list[str] = Field(default_factory=list)


class ExperimentInput(BaseModel):
    name: str
    hypothesis: str
    variable: str
    old_value: str
    new_value: str


class ServiceInput(BaseModel):
    category: str = Field(min_length=1, max_length=80, pattern="^[A-Z0-9_]+$")
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=5000)
    base_price: float = Field(ge=0)
    min_price: float = Field(ge=0)
    max_price: float = Field(ge=0)
    typical_hours: float = Field(gt=0)
    required_skills: list[str] = Field(default_factory=list)
    margin_target: float = Field(default=.3, ge=0, le=1)
    proposal_template: str = ""


class AgencyProfileInput(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=5000)
    positioning: str = ""
    skills: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    advantages: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class CaseStudyInput(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    service: str = Field(min_length=1, max_length=80)
    problem: str = Field(min_length=1, max_length=5000)
    solution: str = Field(min_length=1, max_length=5000)
    result: str = Field(min_length=1, max_length=5000)
    technologies: list[str] = Field(default_factory=list)
    price: float | None = Field(default=None, ge=0)
    duration: str | None = None


class ORMOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
