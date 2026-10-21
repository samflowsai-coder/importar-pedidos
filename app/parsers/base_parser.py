from __future__ import annotations

from abc import ABC, abstractmethod

from app.models.order import Order


class PedidoRecusadoError(ValueError):
    """O parser reconheceu o formato, mas o conteúdo não pode virar pedido sem perda.

    Diferente de devolver `None` (que deixa o GenericParser tentar e gravar lixo):
    o pipeline para, não chama o LLM, e o motivo chega ao operador.
    """


class BaseParser(ABC):
    @abstractmethod
    def parse(self, extracted: dict) -> Order | None:
        pass

    def can_parse(self, extracted: dict) -> bool:
        return True
