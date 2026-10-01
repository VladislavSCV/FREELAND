# Freelance OS

Internal lead qualification, proposal review and project tracking for one small agency. FastAPI, PostgreSQL, Redis and aiogram run as a modular monolith.

The autopilot collects public orders (FL.ru RSS, public Telegram channels, optionally Kwork; optionally via Bright Data), scores them with an LLM on a 1–10 scale and routes them: high scores are approved automatically and arrive in Telegram ready to send, medium scores ask for a one-tap decision, low scores are archived. The system never posts on freelance platforms itself: you send the prepared text and tap «Отправил». See [docs/assumptions.md](docs/assumptions.md).

## Start (local)

1. Copy `.env.example` to `.env`. Set strong random `API_TOKEN` and `WEBHOOK_TOKEN`, an LLM provider and key (`mock` is an offline demo), `TELEGRAM_BOT_TOKEN` and `ALLOWED_TELEGRAM_IDS`. Without a public HTTPS domain use `TELEGRAM_MODE=polling`.
2. `make up-polling` (or `make up-local` without the bot). The API applies migrations on start.
3. `make seed` creates the agency profile and service catalog (`make seed-demo` adds demo leads).
4. Open `http://localhost:8000/docs`. Use `Authorization: Bearer <API_TOKEN>` for API calls.

Production on Timeweb VPS with nginx and Let's Encrypt: [docs/deploy_timeweb.md](docs/deploy_timeweb.md).

## Autopilot

```text
worker (every SCRAPER_INTERVAL_SECONDS)       worker (every minute)
FL.ru RSS / t.me/s/<channel> / Kwork  ──▶  NEW lead ──▶ LLM analysis ──▶ score 1–10
                                                          │
             score ≥ AUTOPILOT_THRESHOLD, autopilot on ──────┼──▶ proposal auto-approved ─▶ TG card [Отправил] [Пропустить]
             score ≥ REVIEW_SCORE ────────────────────────┼──▶ proposal draft ─▶ TG card [Отправить] [Редактировать] [Пропустить]
             score < REVIEW_SCORE ────────────────────────┴──▶ ARCHIVED
```

Telegram commands: `/status` (today's summary), `/autopilot on|off`, `/pay_project <project_id> <amount>`, `/dashboard`, `/leads`, `/pending`, `/newlead`, `/proposals`, `/projects`, `/hours`, `/stats`. «Редактировать» asks you to reply with the new text. Pasted order text can be saved as a lead and is evaluated automatically.

Thresholds and sources are configured in `.env` (`AUTOPILOT_THRESHOLD`, `REVIEW_SCORE`, `AUTOPILOT_ENABLED`, `SCRAPER_SOURCES`, `TELEGRAM_CHANNELS`, `BRIGHTDATA_*`). Run one scrape manually with `make scrape`. API: `GET /api/v1/applications`, `GET|POST /api/v1/autopilot[/on|off]`.

## Workflow

Leads can also be created through `POST /api/v1/leads`, Telegram `/newlead`, file import, or the token-protected webhook. Analyze, generate a proposal, edit it with `PATCH /api/v1/proposals/{id}`, approve, send it manually, then mark it sent. Record `REPLIED`, `WON` or `LOST`; create a project for a won lead and complete it to record economics. The worker prepares a postmortem draft. `/api/v1/analytics/*` shows pipeline and outcomes.

## Payments

`/pay_project <id> <amount>` records a payment, marks the project `PAID` and makes the worker send you a client-facing final report (Markdown). Acquiring is prepared: `POST /api/v1/webhooks/prodamus` (HMAC `Sign` verification) and `POST /api/v1/webhooks/yookassa` (each payment re-checked via the YooKassa API). With `MANUAL_PAYMENT_MODE=true` webhook payments are only recorded; set it to `false` to update projects automatically.

```bash
curl -H 'Authorization: Bearer YOUR_TOKEN' -H 'Content-Type: application/json' \
  -d '{"title":"Telegram support bot","description":"Build a support bot for common customer questions"}' \
  http://localhost:8000/api/v1/leads
```

The CLI supports `freelance lead add`, `freelance lead analyze`, `freelance proposal generate`, `freelance scrape`, `freelance stats`, `freelance knowledge import`, and `freelance health`. Run inside the API container with `docker compose exec api freelance ...`. To import a local research file, run `make knowledge-import FILE=path/to/file.md`.

## Development

Install with `pip install -e '.[dev]'`, then run `pytest` (tests use SQLite and the mock LLM, no network). `ruff check app tests` checks style. For a running Compose stack, `python scripts/smoke.py` exercises the full API workflow. Database tables are managed only by Alembic. The seed is idempotent.

## Security and limitations

The API requires a bearer token; the lead webhook uses an independent token; the Telegram webhook checks Telegram's secret header and the user allowlist; payment webhooks are verified per provider. In production only nginx (80/443) is public. Lead text is treated as untrusted content in prompts and model output is validated. The scraper reads public pages only and does not log in; the system does not deliver proposals on freelance platforms.
