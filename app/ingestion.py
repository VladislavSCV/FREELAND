from typing import Protocol

from app.schemas import LeadInput


class LeadSource(Protocol):
    async def fetch(self) -> list[LeadInput]: ...


class StaticLeadSource:
    """Adapter for manually supplied leads; external adapters can implement LeadSource."""

    def __init__(self, items: list[LeadInput]):
        self.items = items

    async def fetch(self) -> list[LeadInput]:
        return self.items
