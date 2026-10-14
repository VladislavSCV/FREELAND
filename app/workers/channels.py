"""Checks whether public Telegram channels are worth scraping: alive, posting project orders
(not full-time vacancies or ads), and how many posts pass the keyword filter."""
import asyncio
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx

from app.schemas import LeadInput
from app.workers.scraper import Fetcher, KeywordFilter, TelegramChannelSource

ORDER_RE = re.compile(
    r"заказ|нужн[оаы]? (?:сделать|разработ|написать|настроить|бот|сайт|скрипт)|требуется (?:сделать|разработ)|\bтз\b"
    r"|бюджет|💰|оплата|сроки?\b|fixed[- ]price|hourly|budget|\$\s?\d|\d\s?\$|\d[\d ]*\s?(?:₽|руб)"
    r"|looking for (?:a |an )?(?:developer|freelancer|someone)|need (?:a |an )?(?:developer|bot|website|script)|gig\b|bounty",
    re.IGNORECASE,
)
VACANCY_RE = re.compile(
    r"ваканси|полн\w* занятост|full[- ]?time|оклад|зарплат|з/п|salary|в штат|оформлени|\bдмс\b|company name:"
    r"|grades?:|опыт работы от|relocat|\bsenior\b|\bmiddle\b|\bjunior\b",
    re.IGNORECASE,
)


def classify(item: LeadInput) -> str:
    """'order', 'vacancy' or 'other' by simple signals; good enough to rank channels."""
    text = f"{item.title}\n{item.description}"
    orders, vacancies = len(ORDER_RE.findall(text)), len(VACANCY_RE.findall(text))
    if vacancies > orders:
        return "vacancy"
    return "order" if orders else "other"


@dataclass
class ChannelReport:
    channel: str
    exists: bool = False
    last_post: datetime | None = None
    week_posts: int = 0
    week_orders: int = 0
    week_vacancies: int = 0
    week_relevant_orders: int = 0
    examples: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def verdict(self) -> str:
        if self.error:
            return "ошибка"
        if not self.exists:
            return "нет публичного канала"
        if not self.last_post or self.last_post < datetime.now(UTC) - timedelta(days=14):
            return "заброшен"
        if self.week_relevant_orders:
            return "подходит"
        if self.week_vacancies > self.week_orders:
            return "вакансии"
        return "нет подходящих заказов"


async def check_channel(channel: str, fetcher: Fetcher, keywords: KeywordFilter, base_url: str = "https://t.me") -> ChannelReport:
    source = TelegramChannelSource(channel, base_url)
    report = ChannelReport(channel=source.channel)
    try:
        items = await source.fetch(fetcher)
    except (httpx.HTTPError, ValueError) as error:  # network problems are reported per channel
        report.error = type(error).__name__
        return report
    if not items:
        return report
    report.exists = True
    report.last_post = max((i.published_at for i in items if i.published_at), default=None)
    week = [i for i in items if i.published_at and i.published_at > datetime.now(UTC) - timedelta(days=7)]
    report.week_posts = len(week)
    for item in week:
        kind = classify(item)
        report.week_orders += kind == "order"
        report.week_vacancies += kind == "vacancy"
        if kind == "order" and keywords.accepts(item):
            report.week_relevant_orders += 1
            if len(report.examples) < 2:
                report.examples.append(item.title[:70])
    return report


async def check_channels(channels: list[str], fetcher: Fetcher, keywords: KeywordFilter, base_url: str = "https://t.me",
                         concurrency: int = 8) -> list[ChannelReport]:
    semaphore = asyncio.Semaphore(concurrency)

    async def one(channel: str) -> ChannelReport:
        async with semaphore:
            return await check_channel(channel, fetcher, keywords, base_url)

    reports = await asyncio.gather(*(one(c) for c in dict.fromkeys(channels)))
    return sorted(reports, key=lambda r: (-r.week_relevant_orders, -r.week_orders, r.channel))
