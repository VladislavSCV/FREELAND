# Assumptions

- The deployment serves one internal agency. API access uses one bearer token, and Telegram access uses an explicit allowlist of user IDs.
- `LLM_PROVIDER=mock` provides a deterministic local demonstration. It does not claim to be real AI analysis. Set a provider and key for live model output.
- Money is recorded in each lead/project currency. Aggregate revenue is meaningful only when the agency uses one currency; only pricing uses static `CURRENCY_RATES` (see Scraping).
- Project postmortems use deterministic economics and recorded feedback. They never invent qualitative outcomes.
- The generic lead webhook requires a separate shared secret; external platform integrations need individually authorized API credentials and are not configured by default.

## Autopilot (ТЗ «автопилот», 1.txt)

- **Auto-apply = auto-approve + one-tap send.** Kwork and FL.ru have no official API for submitting proposals; automating their login/forms violates platform rules and risks the account. With autopilot on, a lead scoring ≥ `AUTOPILOT_THRESHOLD` gets its proposal approved automatically and arrives in Telegram ready to send (text + order link). The human posts it on the platform and taps «Отправил», which sets `SENT` and schedules a follow-up. `/autopilot on|off` toggles this; nothing is posted to platforms by the system.
- **Score 1–10 is derived, not asked.** The LLM still returns the five 0–100 metrics (§13 of the original spec); the backend computes the weighted overall score and maps it to 1–10 (`round(overall/10)`, clamped). The LLM additionally returns `match_reason`; the proposal text comes from the proposal generator. Both are stored in `applications`.
- When a lead has no client history (no rating, no jobs count), the client weight is moved to fit (+60% of it) and profit (+40%), so unknown marketplace clients do not cap the score. `AUTOPILOT_THRESHOLD` defaults to 7 (`AUTO_APPLY_SCORE` is still accepted).
- `freelance seed` rewrites the agency profile's skills with the current stack on every run; edit `SKILLS` in `app/seed.py` to change it. `make clean-demo` deletes smoke/demo leads (sources `smoke`, `seed`) with their projects; `make archive-pending` archives all applications waiting for review.
- Leads below `REVIEW_SCORE` are archived without generating a proposal to save LLM cost. With autopilot off, every lead at or above `REVIEW_SCORE` goes to review.
- The autopilot evaluates every `NEW` lead, including ones pasted into Telegram or created via API. At most `MAX_EVALUATIONS_PER_TICK` leads per minute; a lead whose analysis failed `MAX_ANALYSIS_ATTEMPTS` times is left in `ANALYSIS_PENDING` for manual handling. A failed proposal generation is retried (`PROPOSAL_PENDING`).
- Editing a proposal from Telegram uses a reply to the bot's prompt (stateless, survives restarts). Only drafts waiting for review can be edited.

## Scraping

- Sources read public data only: FL.ru RSS, public Telegram channel previews (`t.me/s/<channel>`), and the public Kwork project page (off by default: its embedded JSON format is undocumented and may change). One request per source per `SCRAPER_INTERVAL_SECONDS`.
- Bright Data is optional and applies only to sources in `BRIGHTDATA_SOURCES` (default `kwork`; FL.ru RSS and t.me previews work directly and would only add cost). Preferred mode is the Web Unlocker API (`BRIGHTDATA_TOKEN` + `BRIGHTDATA_ZONE`); proxy mode (`BRIGHTDATA_PROXY_URL`) re-signs TLS, so it trusts `BRIGHTDATA_CA_CERT` or skips verification for those public-page requests.
- Orders older than `SCRAPER_MAX_AGE_HOURS` are skipped. Duplicates are detected by `(source, external_id)`, a SHA-256 hash of normalized title+description (across all sources, so the same post in two channels becomes one lead), URL and description similarity. For leads with a platform ID, title-only matching is disabled because channel posts often share generic titles.
- The default seed no longer creates demo leads (`freelance seed --demo` does).
- Pricing defaults to rubles (`DEFAULT_CURRENCY=RUB`, 1500 ₽/h, ruble service catalog). The quoted price is max(LLM estimate, service minimum, hours × rate); when the client's budget is lower, the budget is quoted instead, never below the service minimum, and the Telegram card warns when our price exceeds the budget 1.5×. `freelance seed` switches untouched USD catalog defaults to rubles.
- Catalog prices and `DEFAULT_HOURLY_RATE` are in `DEFAULT_CURRENCY`; for leads in another currency they are converted with `CURRENCY_RATES` (static, manually maintained). If a currency has no rate, catalog limits are not applied and the LLM price is used.

## Telegram webhook

- In production the API process serves `POST /api/v1/telegram/webhook`. Telegram's `X-Telegram-Bot-Api-Secret-Token` must match `TELEGRAM_WEBHOOK_SECRET`, or by default a SHA-256 value derived from `TELEGRAM_BOT_TOKEN`. Updates from users outside `ALLOWED_TELEGRAM_IDS` are acknowledged and dropped; handlers check the allowlist again.
- Updates are processed in a background task so Telegram gets an immediate 200 while LLM-backed actions run. The API runs as one uvicorn process.
- `TELEGRAM_MODE=polling` with the `polling` Compose profile remains for local development without HTTPS.
- `ALLOWED_TELEGRAM_IDS` and the older `TELEGRAM_ALLOWED_USER_IDS` are both accepted.

## Payments

- `/pay_project <id> <amount>` records a manual payment, sets the project to `PAID` (closing it and computing profit/postmortem if it was not completed), and queues a client report. Partial payments still set `PAID`, as the spec requires; `paid_amount` shows how much was received.
- The client report is a deterministic Markdown file (scope, requirements, dates, price, payments) without internal hours, profit or notes. The worker sends it to the allowed Telegram users to forward.
- Prodamus webhooks are verified with HMAC-SHA256 (`Sign` header) over the recursively key-sorted JSON of the form data; projects are referenced by `order_num=project-<id>`. YooKassa notifications are unsigned, so each `payment.succeeded` is confirmed by fetching the payment from the YooKassa API; projects are referenced by `metadata.project_id`. Both are idempotent by provider payment ID.
- With `MANUAL_PAYMENT_MODE=true` verified webhook payments are recorded (`RECORDED`) but do not change project status. «Мой налог» fiscalization is not implemented; the `payments` table is the hook point for it.

## Deployment

- `DATABASE_URL` with `postgresql+asyncpg://` (as in the deploy template) is accepted and converted to the synchronous psycopg driver the app uses.
- The API applies Alembic migrations on start. Revision 0002 is idempotent because 0001 builds the schema from the current models.
- nginx starts with a temporary self-signed certificate and switches to Let's Encrypt automatically once certbot (webroot challenge) issues it.

## Proposals (B2B focus)

- Proposals lead with the business outcome (less manual work, faster replies, data in CRM) and mention only skills relevant to the task; the prompt forbids invented numbers, cases and SLA promises.
- A service's `demo_url` (set with `/demo` or `PATCH /api/v1/services/{category}`) is appended to its proposals as «Пример похожего решения: …». Any other URL the model writes is removed, so links cannot be invented.
- Telegram channels were checked on 2026-10-02: of the suggested ones only `zakazy_it` is public and active; `work_in_it` stopped in 2023, `workforindie` and `ai_developers_jobs` do not exist publicly.

## Services and profile (DevOps / AI infra)

- Catalog: AI_AUTOMATION, TELEGRAM_BOT, LANDING_PAGE plus DEVOPS_INFRA (servers, Docker, CI/CD, hosting moves, Nginx/SSL, monitoring, backups, Kubernetes, Terraform/Ansible; 5 000–150 000 ₽) and AI_INFRA (self-hosted LLM with Ollama/vLLM, RAG, GPU servers; 20 000–300 000 ₽). The LLM chooses the category from the Russian service descriptions; `seed` fills descriptions that still equal the service name and never overwrites edited ones.
- n8n / Make / Zapier are out of scope (a «не делаем» item): integrations are written in Python.
- An explicit REJECT from the model (outside the profile or a «не делаем» item) archives the lead even when the weighted score reaches `REVIEW_SCORE`.
