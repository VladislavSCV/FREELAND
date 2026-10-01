.PHONY: migrate seed seed-demo clean-demo archive-pending reevaluate scrape test lint up up-local up-polling down logs knowledge-import
migrate:
	docker compose run --rm api alembic upgrade head
seed:
	docker compose run --rm api freelance seed
seed-demo:
	docker compose run --rm api freelance seed --demo
# Remove smoke/demo leads and projects from analytics
clean-demo:
	docker compose exec api freelance cleanup-demo
# Archive all applications waiting for review
archive-pending:
	docker compose exec api freelance archive-pending
# Re-run the autopilot on archived leads that pass the keyword filter
reevaluate:
	docker compose exec api freelance reevaluate
scrape:
	docker compose exec api freelance scrape
test:
	python -m pytest
lint:
	ruff check app tests
# Production: everything including nginx + Let's Encrypt (needs DOMAIN pointing to this server)
up:
	docker compose up -d --build
# Local: no nginx/certbot
up-local:
	docker compose up -d --build postgres redis api worker
# Local with Telegram long polling instead of the webhook
up-polling:
	docker compose --profile polling up -d --build postgres redis api worker bot
down:
	docker compose down
logs:
	docker compose logs -f --tail=100 api worker
knowledge-import:
	docker compose run --rm -v "$(PWD)/$(FILE):/tmp/knowledge-input:ro" api freelance knowledge import /tmp/knowledge-input
