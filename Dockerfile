FROM python:3.12-slim
WORKDIR /code
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .
COPY migrations ./migrations
COPY alembic.ini ./
COPY prompts ./prompts
RUN mkdir -p /code/reports
ENV PYTHONPATH=/code
# Migrations are idempotent, so the API applies them on every start.
# One uvicorn process: the Telegram webhook and in-memory bot state live in this process.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'"]
