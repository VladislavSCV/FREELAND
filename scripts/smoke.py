"""Run a local API workflow against the configured Compose stack."""
import json
import os
import urllib.request
import time
from pathlib import Path
from uuid import uuid4

env_file = Path(__file__).resolve().parent.parent / ".env"
env_values = dict(line.split("=", 1) for line in env_file.read_text().splitlines() if "=" in line and not line.startswith("#")) if env_file.exists() else {}
TOKEN = os.environ.get("API_TOKEN", env_values.get("API_TOKEN", ""))
BASE = os.environ.get("API_URL", "http://localhost:8000")


def call(method, path, data=None):
    request = urllib.request.Request(BASE + path, data=json.dumps(data).encode() if data is not None else None, method=method, headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


for attempt in range(30):
    try:
        call("GET", "/health")
        break
    except Exception:
        if attempt == 29:
            raise
        time.sleep(1)

unique_id = str(uuid4())
lead = call("POST", "/api/v1/leads", {"external_id": unique_id, "source": "smoke", "title": f"Telegram order bot {unique_id[:8]}", "description": f"Need a Telegram bot to receive orders and notify staff. Request reference {unique_id}."})["lead"]
lead_id = lead["id"]
analysis = call("POST", f"/api/v1/leads/{lead_id}/analyze")
proposal = call("POST", f"/api/v1/leads/{lead_id}/proposal")
approved = call("POST", f"/api/v1/leads/{lead_id}/approve")
sent = call("POST", f"/api/v1/leads/{lead_id}/mark-sent")
assert approved["status"] == "APPROVED" and sent["status"] == "SENT"
call("POST", f"/api/v1/leads/{lead_id}/outcome/WON")
project = call("POST", "/api/v1/projects", {"lead_id": lead_id})
completed = call("PATCH", f"/api/v1/projects/{project['id']}", {"actual_hours": 9, "external_cost": 30, "status": "COMPLETED"})
dashboard = call("GET", "/api/v1/analytics/dashboard")
print(json.dumps({"lead_id": lead_id, "score": analysis["overall_score"], "proposal_id": proposal["id"], "project_id": project["id"], "profit": completed["profit"], "dashboard_revenue": dashboard["revenue"]}, indent=2))
