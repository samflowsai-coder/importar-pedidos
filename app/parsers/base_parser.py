from __future__ import annotations

from abc import ABC, abstractmethod

from app.models.order import Order


class BaseParser(ABC):
    @abstractmethod
    def parse(self, extracted: dict) -> Order | None:
        pass

    def can_parse(self, extracted: dict) -> bool:
        return True
