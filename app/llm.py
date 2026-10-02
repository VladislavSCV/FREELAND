import asyncio
import json
import time
from typing import Protocol, TypeVar

import httpx
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.models import LLMCall

T = TypeVar("T", bound=BaseModel)


class LLMProvider(Protocol):
    async def generate(self, system: str, data: dict) -> str: ...
    async def structured_generate(self, system: str, data: dict, schema: type[T]) -> T: ...


def _check(response: httpx.Response):
    """raise_for_status with the provider's error text, which explains quota/auth/model problems."""
    if response.is_error:
        raise httpx.HTTPStatusError(f"{response.status_code} from {response.url.host}: {response.text[:300]}", request=response.request, response=response)


def strict_schema(node):
    """Yandex models (YandexGPT 5+) reject schemas with optional fields: mark every property required."""
    if isinstance(node, dict):
        node = {key: strict_schema(value) for key, value in node.items()}
        if isinstance(node.get("properties"), dict):
            node["required"] = list(node["properties"])
        return node
    if isinstance(node, list):
        return [strict_schema(item) for item in node]
    return node


def extract_json(text: str) -> str:
    """Models sometimes wrap JSON in ``` fences or add a sentence around it."""
    text = text.strip()
    if text.startswith("{"):
        return text
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if 0 <= start < end else text


class HTTPProvider:
    def __init__(self, name: str, endpoint: str, key: str, model: str, headers: dict[str, str] | None = None):
        self.name, self.endpoint, self.key, self.model = name, endpoint, key, model
        # OpenAI-format providers differ only in auth headers (Yandex: Api-Key + OpenAI-Project).
        self.headers = headers or {"Authorization": f"Bearer {key}"}
        self.usage = {"input_tokens": 0, "output_tokens": 0}

    def _record_usage(self, input_tokens: int | None, output_tokens: int | None):
        self.usage["input_tokens"] += input_tokens or 0
        self.usage["output_tokens"] += output_tokens or 0

    async def generate(self, system: str, data: dict, schema: dict | None = None) -> str:
        payload = json.dumps(data, ensure_ascii=False)
        # Yandex AI Studio occasionally needs more than 45 s for a full proposal.
        async with httpx.AsyncClient(timeout=90) as client:
            if self.name == "anthropic":
                response = await client.post(self.endpoint, headers={"x-api-key": self.key, "anthropic-version": "2023-06-01"}, json={"model": self.model, "max_tokens": 1600, "system": system, "messages": [{"role": "user", "content": payload}]})
                _check(response)
                body = response.json()
                self._record_usage(body.get("usage", {}).get("input_tokens"), body.get("usage", {}).get("output_tokens"))
                return body["content"][0]["text"]
            if self.name == "gemini":
                response = await client.post(f"{self.endpoint}/{self.model}:generateContent", params={"key": self.key}, json={"system_instruction": {"parts": [{"text": system}]}, "contents": [{"parts": [{"text": payload}]}], "generationConfig": {"responseMimeType": "application/json"}})
                _check(response)
                body = response.json()
                self._record_usage(body.get("usageMetadata", {}).get("promptTokenCount"), body.get("usageMetadata", {}).get("candidatesTokenCount"))
                return body["candidates"][0]["content"]["parts"][0]["text"]
            # Yandex AI Studio enforces a JSON schema; OpenAI/DeepSeek get the portable json_object mode.
            response_format = {"type": "json_schema", "json_schema": {"name": "result", "schema": strict_schema(schema)}} if self.name == "yandex" and schema else {"type": "json_object"}
            response = await client.post(self.endpoint, headers=self.headers, json={"model": self.model, "temperature": 0.3, "response_format": response_format, "messages": [{"role": "system", "content": system}, {"role": "user", "content": payload}]})
            _check(response)
            body = response.json()
            self._record_usage(body.get("usage", {}).get("prompt_tokens"), body.get("usage", {}).get("completion_tokens"))
            return body["choices"][0]["message"]["content"]

    async def structured_generate(self, system: str, data: dict, schema: type[T]) -> T:
        json_schema = schema.model_json_schema()
        instruction = system + "\n\nReturn one JSON object with your answer. Its format is described by this JSON Schema (describes the fields; do not return the schema itself): " + json.dumps(json_schema, ensure_ascii=False)
        last_error = None
        for attempt in range(3):
            try:
                return schema.model_validate_json(extract_json(await self.generate(instruction, data, json_schema)))
            except (httpx.HTTPError, ValueError, KeyError, IndexError) as error:
                last_error = error
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
        raise RuntimeError(f"LLM request failed: {type(last_error).__name__}: {last_error}")


class MockProvider:
    async def generate(self, system: str, data: dict) -> str:
        return json.dumps({"message": "Mock provider supports structured demo output only"})

    async def structured_generate(self, system: str, data: dict, schema: type[T]) -> T:
        if "project" in data:
            project = data["project"]
            underestimated = project["actual_hours"] > project["estimated_hours"]
            return schema.model_validate({"went_well": ["Project was completed"], "went_wrong": ["Actual hours exceeded estimate"] if underestimated else [], "underestimated_areas": ["Total effort"] if underestimated else [], "overestimated_areas": ["Total effort"] if project["actual_hours"] < project["estimated_hours"] else [], "recommended_price_next_time": max(0, project["contract_price"] + (project["actual_hours"] - project["estimated_hours"]) * settings().default_hourly_rate), "process_change": "Review scope and estimate before quoting"})
        if "lead" in data and "services" in data:
            lead = data["lead"]
            content = (lead["title"] + " " + lead["description"]).lower()
            category = "TELEGRAM_BOT" if "telegram" in content or "бот" in content else "LANDING_PAGE" if "landing" in content or "лендинг" in content else "AI_AUTOMATION"
            service = next((s for s in data["services"] if s["category"] == category), None)
            hours = service["typical_hours"] if service else 12
            price = max(hours * settings().default_hourly_rate, service["min_price"] if service else 0)
            return schema.model_validate({"service_category": category, "match_reason": "Демо-оценка: задача похожа на услугу из каталога", "fit_score": 75, "profit_score": 70, "complexity_score": 45, "risk_score": 25, "client_quality_score": 50, "estimated_hours": hours, "recommended_price": price, "currency": lead["currency"], "recommended_action": "APPLY", "reasons": ["Service matches agency catalog", "Estimate uses configured hourly rate"], "risks": ["Requirements need confirmation"], "missing_information": ["Detailed scope and acceptance criteria"], "required_skills": []})
        lead = data["lead"]
        price = data["analysis"]["recommended_price"]
        body = f"Здравствуйте! По задаче «{lead['title']}» предлагаю сначала уточнить объём работ, затем реализовать решение и проверить его вместе с вами. Оценка: {price:g} {data['pricing_rules']['currency']}. Можем обсудить требования и согласовать следующий шаг?"
        return schema.model_validate({"body": body, "questions": ["Какие критерии приёмки и сроки?"], "assumptions": ["Объём работ будет подтверждён до начала"], "approach": "Уточнение, реализация, проверка", "suggested_price": price, "delivery_estimate": 7})


def provider() -> LLMProvider:
    cfg = settings()
    if cfg.llm_provider == "mock":
        return MockProvider()
    if cfg.llm_provider == "yandex":
        if not cfg.yandex_api_key or not cfg.yandex_folder_id:
            raise ValueError("YANDEX_API_KEY and YANDEX_FOLDER_ID are required for yandex")
        # Yandex AI Studio OpenAI-compatible API; models are addressed as gpt://<folder>/<model>.
        model = cfg.llm_model if cfg.llm_model.startswith("gpt://") else f"gpt://{cfg.yandex_folder_id}/{cfg.llm_model}"
        endpoint = (cfg.llm_base_url or "https://ai.api.cloud.yandex.net/v1").rstrip("/") + "/chat/completions"
        return HTTPProvider("yandex", endpoint, cfg.yandex_api_key, model, headers={"Authorization": f"Api-Key {cfg.yandex_api_key}", "OpenAI-Project": cfg.yandex_folder_id})
    choices = {"openai": ("https://api.openai.com/v1/chat/completions", cfg.openai_api_key), "deepseek": ("https://api.deepseek.com/chat/completions", cfg.deepseek_api_key), "anthropic": ("https://api.anthropic.com/v1/messages", cfg.anthropic_api_key), "gemini": ("https://generativelanguage.googleapis.com/v1beta/models", cfg.gemini_api_key)}
    if cfg.llm_provider not in choices:
        raise ValueError("Unknown LLM provider")
    endpoint, key = choices[cfg.llm_provider]
    if cfg.llm_base_url and cfg.llm_provider in {"openai", "deepseek"}:
        # Any OpenAI-compatible gateway, e.g. a proxy reachable from a Russian VPS.
        endpoint = cfg.llm_base_url.rstrip("/") + "/chat/completions"
    if not key:
        raise ValueError(f"Missing API key for {cfg.llm_provider}")
    # DeepSeek and other OpenAI-compatible APIs share the OpenAI request format.
    return HTTPProvider("openai" if cfg.llm_provider == "deepseek" else cfg.llm_provider, endpoint, key, cfg.llm_model)


async def observed_generate[T: BaseModel](db: Session, purpose: str, system: str, data: dict, schema: type[T]) -> T:
    started = time.monotonic()
    record = LLMCall(provider=settings().llm_provider, model=settings().llm_model, purpose=purpose)
    client: LLMProvider | None = None
    try:
        client = provider()
        result = await client.structured_generate(system, data, schema)
        record.success = True
        return result
    except Exception as error:
        record.error = str(error)[:2000]
        raise
    finally:
        usage = getattr(client, "usage", None) or {}
        record.input_tokens = usage.get("input_tokens", 0)
        record.output_tokens = usage.get("output_tokens", 0)
        record.latency = time.monotonic() - started
        db.add(record)
        db.commit()
