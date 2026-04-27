from __future__ import annotations

import json
import os
import re

from app.llm.openrouter_client import LLMUnavailableError, OpenRouterClient
from app.models.order import Order, OrderHeader, OrderItem
from app.utils.logger import logger

# Model is configurable — swap without redeployment via OPENROUTER_MODEL env var.
# Default: Gemini Flash (cheap, strong multilingual, reliable JSON).
# Alternatives: "anthropic/claude-haiku-4-5-20251001", "anthropic/claude-haiku-3-5"
DEFAULT_MODEL = "google/gemini-flash-1.5"
MAX_TEXT_CHARS = 8000

_PROMPT = """\
Você receberá o texto extraído de um pedido de compra (PDF ou planilha) de um fornecedor \
de calçados brasileiro. Extraia os dados e retorne SOMENTE um objeto JSON válido, \
sem texto adicional, no seguinte formato:

{
  "header": {
    "order_number": "string ou null",
    "issue_date": "DD/MM/YYYY ou null",
    "customer_name": "string ou null",
    "customer_cnpj": "string ou null"
  },
  "items": [
    {
      "description": "string ou null",
      "product_code": "string ou null",
      "ean": "string ou null",
      "quantity": número ou null,
      "unit_price": número ou null,
      "total_price": número ou null,
      "delivery_date": "DD/MM/YYYY ou null",
      "delivery_cnpj": "string ou null",
      "delivery_name": "string ou null",
      "obs": "string ou null"
    }
  ]
}

Regras:
- Datas sempre no formato DD/MM/YYYY.
- Números decimais usando ponto (ex: 1500.50).
- EAN deve ter 13 dígitos; se não encontrar, use null.
- Se um campo não existir no documento, use null (nunca omita o campo).
- Retorne apenas o JSON, sem markdown, sem explicações.

Pedido:
"""


def _extract_json(text: str) -> dict:
    """Extract JSON from response even if the model wraps it in markdown fences."""
    # Strip markdown code fences if present
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        return json.loads(match.group(1))
    # Try direct parse
    return json.loads(text.strip())


class LLMFallbackParser:
    """Last-resort parser. Lazy-instantiates OpenRouter client on first use.

    Tests inject a fake by setting `parser._client` (an `OpenRouterClient`-like
    object exposing `chat_completion(...)` -> str).
    """

    def __init__(self) -> None:
        self._client: OpenRouterClient | None = None

    @property
    def model(self) -> str:
        return os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL)

    @property
    def client(self) -> OpenRouterClient:
        if self._client is None:
            self._client = OpenRouterClient.from_env()
        return self._client

    def parse(self, extracted: dict, source_file: str = "") -> Order | None:
        text = extracted.get("text", "")
        if not text.strip():
            return None

        logger.info(f"LLM fallback [{self.model}]: {source_file}")

        try:
            content = self.client.chat_completion(
                model=self.model,
                messages=[{"role": "user", "content": _PROMPT + text[:MAX_TEXT_CHARS]}],
                response_format={"type": "json_object"},
                max_tokens=2048,
                temperature=0,
            )
            data = _extract_json(content or "")

            header = OrderHeader(**{
                k: v for k, v in data.get("header", {}).items()
                if k in OrderHeader.model_fields
            })
            items = [
                OrderItem(**{k: v for k, v in item.items() if k in OrderItem.model_fields})
                for item in data.get("items", [])
                if isinstance(item, dict)
            ]
            return Order(header=header, items=items, source_file=source_file)

        except LLMUnavailableError as e:
            logger.error(f"LLM fallback indisponível [{source_file}]: {e}")
            return None
        except Exception as e:  # noqa: BLE001 — JSON parse errors, validation, etc.
            logger.error(f"LLM fallback falhou [{source_file}]: {e}")
            return None
