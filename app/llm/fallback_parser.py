from __future__ import annotations

import json
import os
import re
from typing import Optional

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
    def __init__(self) -> None:
        self._client: Optional[object] = None

    @property
    def model(self) -> str:
        return os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL)

    @property
    def client(self):
        if self._client is None:
            import openai
            self._client = openai.OpenAI(
                base_url="https://openrouter.ai/api/v1",
                api_key=os.environ["OPENROUTER_API_KEY"],
            )
        return self._client

    def parse(self, extracted: dict, source_file: str = "") -> Optional[Order]:
        text = extracted.get("text", "")
        if not text.strip():
            return None

        logger.info(f"LLM fallback [{self.model}]: {source_file}")

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                max_tokens=2048,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[{"role": "user", "content": _PROMPT + text[:MAX_TEXT_CHARS]}],
                extra_headers={"X-Title": "importar-pedidos"},
            )
            content = response.choices[0].message.content or ""
            data = _extract_json(content)

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

        except Exception as e:
            logger.error(f"LLM fallback falhou [{source_file}]: {e}")
            return None
