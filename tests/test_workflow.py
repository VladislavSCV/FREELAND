import pytest
from fastapi import HTTPException

from app.models import AgencyProfile, Project, Service
from app.schemas import AnalysisResult, LeadInput
from app.services import create_lead, economics, score, transition
from app.worker import tick


def test_scoring_and_economics():
    result = AnalysisResult(service_category="AI_AUTOMATION", fit_score=80, profit_score=70, complexity_score=40, risk_score=20, client_quality_score=50, estimated_hours=10, recommended_price=500, currency="USD", recommended_action="APPLY", reasons=["fit"], risks=[], missing_information=[], required_skills=[])
    assert score(result) == 71.5
    # No client history: 20% client weight -> fit +12%, profit +8%.
    assert score(result, client_known=False) == round(80 * .47 + 70 * .33 + 80 * .20, 2)
    assert economics(10, 500, 50)["effective_hourly_rate"] == 45
    with pytest.raises(ValueError):
        economics(0, 500)


def test_dedup_and_transitions(db):
    lead, created = create_lead(db, LeadInput(title="Same job", description="A sufficiently clear job", source="test"))
    assert created
    same, created = create_lead(db, LeadInput(title="Same job", description="Other description", source="test"))
    assert not created and same.id == lead.id
    with pytest.raises(HTTPException):
        transition(lead, "SENT")


def test_full_workflow(client, db, monkeypatch):
    db.add(Service(category="TELEGRAM_BOT", name="Telegram bot", base_price=450, min_price=250, max_price=4000, typical_hours=10))
    db.add(AgencyProfile(name="Agency", description="Builds bots", skills=["Python"], technologies=[]))
    db.commit()
    response = client.post("/api/v1/leads", json={"title": "Telegram bot", "description": "Need a Telegram bot for support"})
    assert response.status_code == 200
    lead_id = response.json()["lead"]["id"]
    assert client.post(f"/api/v1/leads/{lead_id}/mark-sent").status_code == 409
    assert client.post(f"/api/v1/leads/{lead_id}/analyze").status_code == 200
    proposal = client.post(f"/api/v1/leads/{lead_id}/proposal")
    assert proposal.status_code == 200
    assert proposal.json()["status"] == "DRAFT"
    edited = client.patch(f"/api/v1/proposals/{proposal.json()['id']}", json={"body": "Edited proposal for this specific Telegram support bot project."})
    assert edited.status_code == 200
    assert client.post(f"/api/v1/leads/{lead_id}/approve").status_code == 200
    assert client.patch(f"/api/v1/proposals/{proposal.json()['id']}", json={"body": "Late edit is blocked after approval."}).status_code == 409
    assert client.post(f"/api/v1/leads/{lead_id}/mark-sent").status_code == 200
    assert client.post(f"/api/v1/leads/{lead_id}/outcome/WON").status_code == 200
    project = client.post("/api/v1/projects", json={"lead_id": lead_id})
    assert project.status_code == 200
    project_id = project.json()["id"]
    completed = client.patch(f"/api/v1/projects/{project_id}", json={"actual_hours": 9, "external_cost": 30, "status": "COMPLETED"})
    assert completed.status_code == 200
    assert completed.json()["postmortem"]["profit"] > 0
    assert completed.json()["postmortem"]["ai_status"] == "PENDING"
    monkeypatch.setattr("app.worker.SessionLocal", lambda: db)
    asyncio.run(tick())
    assert db.get(Project, project_id).postmortem["ai_status"] == "READY"
    assert client.get("/api/v1/analytics/dashboard").json()["revenue"] > 0
import asyncio
