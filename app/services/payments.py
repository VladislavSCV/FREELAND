"""Payment accounting: manual entries from Telegram and Prodamus / YooKassa webhooks."""
import hashlib
import hmac
import json
import re
from uuid import uuid4

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Payment, Project, now
from app.services import audit, close_project


def mark_project_paid(db: Session, project: Project, amount: float):
    if project.status == "CANCELLED":
        raise HTTPException(409, "Project is cancelled")
    if amount <= 0:
        raise HTTPException(422, "Amount must be positive")
    project.paid_amount = round((project.paid_amount or 0) + amount, 2)
    project.paid_at = now()
    if project.completed_at is None or not project.postmortem:
        close_project(project)
    project.status = "PAID"
    project.report_status = "PENDING"


def register_payment(db: Session, *, provider: str, external_id: str, project_id: int | None, amount: float, currency: str, raw: dict, apply: bool) -> Payment:
    """Idempotent per (provider, external_id). Applies to the project only when `apply` is true."""
    existing = db.scalar(select(Payment).where(Payment.provider == provider, Payment.external_id == external_id))
    if existing:
        return existing
    project = db.get(Project, project_id) if project_id else None
    payment = Payment(provider=provider, external_id=external_id, project_id=project.id if project else None, amount=amount, currency=currency, raw_payload=raw, status="UNMATCHED" if project is None else "APPLIED" if apply else "RECORDED")
    if project is not None and apply:
        mark_project_paid(db, project, amount)
    db.add(payment)
    db.flush()
    audit(db, "payment", payment.id, payment.status.lower(), {"provider": provider, "project_id": payment.project_id, "amount": amount})
    db.commit()
    return payment


def manual_payment(db: Session, project_id: int, amount: float) -> Payment:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    return register_payment(db, provider="manual", external_id=f"manual-{uuid4()}", project_id=project.id, amount=amount, currency=project.currency, raw={}, apply=True)


def project_id_from(value: object) -> int | None:
    """Payments reference projects as order number / metadata `project-<id>` or plain `<id>`."""
    match = re.fullmatch(r"(?:project[-_:])?(\d+)", str(value or "").strip(), re.IGNORECASE)
    return int(match.group(1)) if match else None


# --- Prodamus -------------------------------------------------------------------------------

def _nest(form: dict[str, str]) -> dict:
    """Turn PHP-style keys (`products[0][name]`) into the nested structure Prodamus signs."""
    root: dict = {}
    for key, value in form.items():
        parts = re.findall(r"[^\[\]]+", key)
        node = root
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        if parts:
            node[parts[-1]] = value
    return root


def _canonical(node):
    if isinstance(node, dict):
        items = {k: _canonical(v) for k, v in sorted(node.items())}
        if items and all(k.isdigit() for k in items) and sorted(map(int, items)) == list(range(len(items))):
            return [items[str(i)] for i in range(len(items))]
        return items
    if isinstance(node, list):
        return [_canonical(x) for x in node]
    return str(node)


def prodamus_signature(data: dict, secret: str) -> str:
    payload = json.dumps(_canonical(data), ensure_ascii=False, separators=(",", ":")).replace("/", "\\/")
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def handle_prodamus(db: Session, form: dict[str, str], sign: str | None) -> Payment | None:
    secret = settings().prodamus_secret_key
    if not secret:
        raise HTTPException(503, "Prodamus is not configured")
    data = _nest(form)
    if not sign or not hmac.compare_digest(prodamus_signature(data, secret), sign.lower()):
        raise HTTPException(403, "Invalid Prodamus signature")
    if data.get("payment_status") != "success":
        return None
    try:
        amount = float(str(data.get("sum", "0")).replace(",", "."))
    except ValueError as error:
        raise HTTPException(422, "Invalid sum") from error
    return register_payment(db, provider="prodamus", external_id=str(data.get("order_id") or data.get("order_num")), project_id=project_id_from(data.get("order_num")), amount=amount, currency=str(data.get("currency", "RUB")).upper()[:3], raw=data, apply=not settings().manual_payment_mode)


# --- YooKassa -------------------------------------------------------------------------------

async def fetch_yookassa_payment(payment_id: str) -> dict:
    cfg = settings()
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(f"https://api.yookassa.ru/v3/payments/{payment_id}", auth=(cfg.yookassa_shop_id, cfg.yookassa_secret_key))
        response.raise_for_status()
        return response.json()


async def handle_yookassa(db: Session, body: dict) -> Payment | None:
    cfg = settings()
    if not cfg.yookassa_shop_id or not cfg.yookassa_secret_key:
        raise HTTPException(503, "YooKassa is not configured")
    if body.get("event") != "payment.succeeded":
        return None
    payment_id = str((body.get("object") or {}).get("id") or "")
    if not re.fullmatch(r"[\w-]{1,64}", payment_id):
        raise HTTPException(422, "Invalid payment id")
    # Notifications are unsigned; the payment is confirmed by reading it back from the YooKassa API.
    try:
        verified = await fetch_yookassa_payment(payment_id)
    except httpx.HTTPError as error:
        raise HTTPException(502, "Could not verify payment with YooKassa") from error
    if verified.get("status") != "succeeded" or not verified.get("paid", True):
        raise HTTPException(409, "Payment is not succeeded")
    amount = verified.get("amount") or {}
    metadata = verified.get("metadata") or {}
    return register_payment(db, provider="yookassa", external_id=payment_id, project_id=project_id_from(metadata.get("project_id")), amount=float(amount.get("value", 0)), currency=str(amount.get("currency", "RUB"))[:3], raw=verified, apply=not cfg.manual_payment_mode)
