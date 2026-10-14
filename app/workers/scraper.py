"""Background collection of public freelance orders.

Sources read only public data: the FL.ru RSS feed, public Telegram channel previews
(t.me/s/<channel>) and the public Kwork project list. Requests can go through Bright Data
(a proxy URL or the Web Unlocker API) when configured. Nothing here logs in or posts.
"""
import asyncio
import html
import json
import logging
import re
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Protocol
from xml.etree import ElementTree

import httpx
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, settings
from app.models import Lead
from app.schemas import LeadInput
from app.services import convert, create_lead

log = logging.getLogger(__name__)
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
CURRENCIES = {"₽": "RUB", "руб": "RUB", "р.": "RUB", "rub": "RUB", "$": "USD", "usd": "USD", "€": "EUR", "eur": "EUR",
              "₴": "UAH", "грн": "UAH", "uah": "UAH"}
BUDGET_RE = re.compile(r"(?:бюджет|оплата|цена|стоимость|budget)\s*[:\-–—]?\s*(?:до|от|~)?\s*([$€]?)\s*(\d[\d  .,]*)\s*(к|k|тыс\.?)?\s*(₽|руб\.?|р\.|rub|\$|usd|€|eur|₴|грн\.?|uah)?", re.IGNORECASE)


class Fetcher:
    """HTTP access. Sources listed in BRIGHTDATA_SOURCES go through Bright Data (Web Unlocker API
    when BRIGHTDATA_TOKEN + BRIGHTDATA_ZONE are set, otherwise BRIGHTDATA_PROXY_URL); others go direct."""

    def __init__(self, cfg: Settings, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        options = {"timeout": 60, "follow_redirects": True, "headers": {"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.8"}}
        self.client = client or httpx.AsyncClient(**options)
        self.proxy_client = None
        if cfg.brightdata_proxy_url and not client:
            # The Bright Data proxy re-signs TLS; trust its CA when provided (only public pages are fetched).
            self.proxy_client = httpx.AsyncClient(proxy=cfg.brightdata_proxy_url, verify=cfg.brightdata_ca_cert or False, **options)
        self.routed = {x.strip() for x in cfg.brightdata_sources.split(",") if x.strip()}

    def uses_brightdata(self, source: str) -> bool:
        configured = bool(self.cfg.brightdata_token and self.cfg.brightdata_zone) or bool(self.cfg.brightdata_proxy_url)
        return configured and ("*" in self.routed or source in self.routed)

    async def get(self, url: str, source: str = "") -> str:
        if self.uses_brightdata(source) and self.cfg.brightdata_token and self.cfg.brightdata_zone:
            response = await self.client.post("https://api.brightdata.com/request", headers={"Authorization": f"Bearer {self.cfg.brightdata_token}"}, json={"zone": self.cfg.brightdata_zone, "url": url, "format": "raw"})
        elif self.uses_brightdata(source) and self.proxy_client is not None:
            response = await self.proxy_client.get(url)
        else:
            response = await self.client.get(url)
        if response.is_error:
            raise httpx.HTTPStatusError(f"{response.status_code} for {url}: {response.text[:200]}", request=response.request, response=response)
        return response.text

    async def aclose(self):
        await self.client.aclose()
        if self.proxy_client is not None:
            await self.proxy_client.aclose()


class OrderSource(Protocol):
    name: str

    async def fetch(self, fetcher: Fetcher) -> list[LeadInput]: ...


def clean_html(value: str) -> str:
    value = re.sub(r"<br\s*/?>|</p>|</div>", "\n", value, flags=re.IGNORECASE)
    value = html.unescape(re.sub(r"<[^>]+>", "", value))
    return "\n".join(line.strip() for line in value.splitlines() if line.strip())


MONEY_EMOJI_RE = re.compile(r"💰\s*(?:до|от)?\s*([$€]?)\s*(\d[\d \u00a0.,]*)\s*(к|k|тыс\.?)?\s*(₽|руб\.?|р\.|rub|\$|usd|€|eur|₴|грн\.?|uah)?", re.IGNORECASE)
MENTION_HANDLE_RE = re.compile(r"(?<![\w/@])@([A-Za-z][A-Za-z0-9_]{4,31})\b")
TG_PROFILE_LINK_RE = re.compile(r'href="https?://(?:t\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]{4,31})/?(?:\?[^"]*)?"')
KWORK_LINK_RE = re.compile(r'href="(https?://kwork\.ru/projects/(\d+)[^"]*)"')


def parse_budget(text: str) -> tuple[float | None, str | None]:
    match = BUDGET_RE.search(text) or MONEY_EMOJI_RE.search(text)
    if not match:
        return None, None
    prefix, raw_number, thousands, suffix = match.groups()
    digits = re.sub(r"[  ,]", "", raw_number).rstrip(".")
    try:
        amount = float(digits)
    except ValueError:
        return None, None
    if thousands:
        amount *= 1000
    marker = (prefix or suffix or "").lower().rstrip(".")
    currency = next((code for key, code in CURRENCIES.items() if marker and key.rstrip(".") == marker), None)
    return (amount if amount > 0 else None), currency


# Service lines of aggregator/mirror posts that are not the task title.
META_LINE_RE = re.compile(r"💰|⏰|📁|Групп[аы]:|Group:|^#|^(?:🆕|💬|📌|🛠|👥|🔔)?\s*(?:NEW PROJECT|НОВЫЙ ЗАКАЗ)", re.IGNORECASE)


def title_from(text: str) -> str:
    for line in text.splitlines():
        line = line.strip(" #*-•📌💬📝🔔🆕📢")
        if len(line) >= 5 and not META_LINE_RE.search(line):
            return line[:200]
    return text.strip()[:200]


class FlRssSource:
    name = "fl_ru"

    def __init__(self, url: str):
        self.url = url

    async def fetch(self, fetcher: Fetcher) -> list[LeadInput]:
        return list(self.parse(await fetcher.get(self.url, "fl_rss")))

    def parse(self, xml_text: str) -> Iterator[LeadInput]:
        root = ElementTree.fromstring(xml_text.encode())
        for item in root.iter("item"):
            title = clean_html(item.findtext("title") or "")
            description = clean_html(item.findtext("description") or "") or title
            link = (item.findtext("link") or "").strip()
            guid = (item.findtext("guid") or link).strip()
            project_id = re.search(r"/projects/(\d+)", link) or re.search(r"(\d{5,})", guid)
            budget, currency = parse_budget(f"{title}\n{description}")
            published = item.findtext("pubDate")
            title = re.sub(r"\s*\(Бюджет:[^)]*\)\s*$", "", title).strip() or title
            try:
                yield LeadInput(source=self.name, external_id=project_id.group(1) if project_id else guid[:255], url=link or None, title=title[:500], description=description[:20000], budget_max=budget, currency=currency or "RUB", published_at=parsedate_to_datetime(published) if published else None, raw_payload={"source_platform": self.name, "category": item.findtext("category")})
            except (ValidationError, ValueError, TypeError):
                log.warning("Skipping malformed FL.ru item %s", link)


class TelegramChannelSource:
    def __init__(self, channel: str, base_url: str = "https://t.me"):
        self.channel = channel.strip().lstrip("@").removeprefix("https://t.me/").strip("/")
        self.name = f"tg_{self.channel}"[:80]
        # TELEGRAM_API_URL (the Cloudflare Worker) also mirrors t.me/s/ for servers where t.me is blocked.
        self.base_url = base_url.rstrip("/")

    async def fetch(self, fetcher: Fetcher) -> list[LeadInput]:
        return list(self.parse(await fetcher.get(f"{self.base_url}/s/{self.channel}", "telegram")))

    @staticmethod
    def channel_handles(page: str, min_posts: int = 3) -> list[str]:
        """Usernames repeated across several posts are the channel's own signature (admin, ads,
        referral links), never a client's contact."""
        counts: Counter[str] = Counter()
        for chunk in page.split('data-post="')[1:]:
            body = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', chunk, re.DOTALL)
            if body:
                names = {m.lower() for m in MENTION_HANDLE_RE.findall(clean_html(body.group(1)))}
                names |= {m.lower() for m in TG_PROFILE_LINK_RE.findall(body.group(1))}
                counts.update(names)
        return sorted(name for name, count in counts.items() if count >= min_posts)

    def parse(self, page: str) -> Iterator[LeadInput]:
        signature = self.channel_handles(page)
        for chunk in page.split('data-post="')[1:]:
            post = chunk.split('"', 1)[0]
            body = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', chunk, re.DOTALL)
            if not body or "/" not in post:
                continue
            text = clean_html(body.group(1))
            if len(text) < 40:
                continue
            stamp = re.search(r'<time[^>]*datetime="([^"]+)"', chunk)
            budget, currency = parse_budget(text)
            # Mirror/aggregator posts start with service lines (category, budget, source group); skip them.
            title = title_from(text)
            # Posts that mirror a Kwork project are stored as that project, so they merge with the Kwork feed.
            kwork = KWORK_LINK_RE.search(body.group(1)) or KWORK_LINK_RE.search(chunk)
            # Contacts hidden behind link text ("Написать заказчику") are only visible in the HTML.
            contacts = [c for c in dict.fromkeys(TG_PROFILE_LINK_RE.findall(body.group(1))) if c.lower() not in signature]
            source, external_id, url = ("kwork", kwork.group(2), f"https://kwork.ru/projects/{kwork.group(2)}") if kwork else (self.name, post[:255], f"https://t.me/{post}")
            try:
                yield LeadInput(source=source, external_id=external_id, url=url, title=title[:500], description=text[:20000], budget_max=budget, currency=currency or "RUB", published_at=datetime.fromisoformat(stamp.group(1)) if stamp else None, raw_payload={"source_platform": "telegram", "channel": self.channel, "post": post, "contacts": contacts, "channel_handles": signature})
            except (ValidationError, ValueError):
                log.warning("Skipping malformed Telegram post %s", post)


class KworkSource:
    """Public Kwork project list. The page embeds its data as JSON; the parser looks for the
    list of projects by shape so small markup changes do not break it."""

    name = "kwork"

    def __init__(self, url: str):
        self.url = url

    async def fetch(self, fetcher: Fetcher) -> list[LeadInput]:
        return list(self.parse(await fetcher.get(self.url, "kwork")))

    @staticmethod
    def _projects(node) -> Iterator[dict]:
        if isinstance(node, list):
            if node and all(isinstance(x, dict) and {"id", "name", "description"} <= x.keys() for x in node):
                yield from node
                return
            for value in node:
                yield from KworkSource._projects(value)
        elif isinstance(node, dict):
            for value in node.values():
                yield from KworkSource._projects(value)

    def parse(self, page: str) -> Iterator[LeadInput]:
        decoder = json.JSONDecoder()
        seen: set[str] = set()
        for marker in re.finditer(r"(?:window\.stateData|window\.__INITIAL_STATE__)\s*=\s*", page):
            try:
                data, _ = decoder.raw_decode(page, marker.end())
            except json.JSONDecodeError:
                continue
            for project in self._projects(data):
                project_id = str(project["id"])
                if project_id in seen:
                    continue
                seen.add(project_id)
                price = project.get("priceLimit") or project.get("possiblePriceLimit")
                created = project.get("date_create") or project.get("dateCreate")
                try:
                    yield LeadInput(source=self.name, external_id=project_id, url=f"https://kwork.ru/projects/{project_id}", title=clean_html(str(project["name"]))[:500], description=clean_html(str(project["description"]))[:20000] or str(project["name"]), budget_max=float(price) if price else None, currency="RUB", published_at=datetime.fromisoformat(str(created).replace(" ", "T")).replace(tzinfo=UTC) if created else None, raw_payload={"source_platform": self.name})
                except (ValidationError, ValueError, TypeError):
                    log.warning("Skipping malformed Kwork project %s", project_id)


def _split(value: str) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


def _pattern(words: list[str]) -> re.Pattern | None:
    return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(w.casefold()) for w in words) + ")") if words else None


class KeywordFilter:
    """Cheap relevance gate so the LLM only sees orders that can match the agency's services."""

    def __init__(self, include: str, exclude: str):
        self.include = _pattern(_split(include))
        self.exclude = _pattern(_split(exclude))

    def accepts(self, item: LeadInput) -> bool:
        title = item.title.casefold()
        # An exclude word in the title wins unless the title also names our kind of work
        # ("Python-скрипт загрузки видео" stays, "Монтаж видео" goes); the LLM judges the rest.
        if self.exclude and self.exclude.search(title) and not (self.include and self.include.search(title)):
            return False
        return not self.include or bool(self.include.search(f"{title}\n{item.description.casefold()}"))


def configured_sources(cfg: Settings) -> list[OrderSource]:
    enabled = {x.strip() for x in cfg.scraper_sources.split(",") if x.strip()}
    sources: list[OrderSource] = []
    if "fl_rss" in enabled:
        sources.extend(FlRssSource(url) for url in _split(cfg.fl_rss_url))
    if "kwork" in enabled:
        sources.extend(KworkSource(url) for url in _split(cfg.kwork_projects_url))
    if "telegram" in enabled:
        sources.extend(TelegramChannelSource(x, cfg.telegram_api_url or "https://t.me") for x in cfg.telegram_channels.split(",") if x.strip())
    return sources


def worth_it(item: LeadInput, min_budget: float) -> bool:
    """Orders with a stated budget below MIN_BUDGET (in DEFAULT_CURRENCY) are not worth a proposal.
    Orders without a budget pass: the LLM judges them by the task itself."""
    budget = item.budget_max or item.budget_min
    if not budget or min_budget <= 0:
        return True
    threshold = convert(min_budget, item.currency)
    return threshold is None or budget >= threshold


def store(db: Session, items: list[LeadInput], max_age_hours: int, keywords: KeywordFilter | None = None,
          min_budget: float = 0) -> int:
    """Save new relevant orders with status NEW; known external IDs are skipped before full dedup.
    Filtered-out orders are not stored, so changing the filters applies to them on the next scrape."""
    if keywords:
        items = [item for item in items if keywords.accepts(item)]
    items = [item for item in items if worth_it(item, min_budget)]
    if not items:
        return 0
    cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)
    known = {(row.source, row.external_id) for row in db.execute(select(Lead.source, Lead.external_id).where(Lead.external_id.in_([x.external_id for x in items if x.external_id])))}
    created = 0
    for item in items:
        if (item.source, item.external_id) in known:
            continue
        published = item.published_at
        if published and (published if published.tzinfo else published.replace(tzinfo=UTC)) < cutoff:
            continue
        _, fresh = create_lead(db, item)
        created += fresh
    return created


async def scrape(db: Session, sources: list[OrderSource] | None = None, fetcher: Fetcher | None = None) -> dict[str, int]:
    cfg = settings()
    sources = configured_sources(cfg) if sources is None else sources
    own_fetcher = fetcher is None
    fetcher = fetcher or Fetcher(cfg)
    keywords = KeywordFilter(cfg.scraper_include_keywords, cfg.scraper_exclude_keywords)
    stats: dict[str, int] = {}
    try:
        for source in sources:
            try:
                items = await source.fetch(fetcher)
                relevant = sum(keywords.accepts(item) and worth_it(item, cfg.min_budget) for item in items)
                created = store(db, items, cfg.scraper_max_age_hours, keywords, cfg.min_budget)
                stats[source.name] = stats.get(source.name, 0) + created
                log.info("Scraped %s: %s items, %s relevant, %s new", source.name, len(items), relevant, created)
            except Exception:
                db.rollback()
                log.exception("Scraping %s failed", source.name)
                stats[source.name] = -1
            await asyncio.sleep(1)
    finally:
        if own_fetcher:
            await fetcher.aclose()
    return stats
