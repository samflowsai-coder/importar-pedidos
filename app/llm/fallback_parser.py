from __future__ import annotations

import json
import os
from typing import Optional

from app.models.order import Order, OrderHeader, OrderItem
from app.utils.logger import logger

MODEL = "claude-haiku-4-5-20251001"
MAX_TEXT_CHARS = 4000


class LLMFallbackParser:
    def __init__(self):
        self._client = None

    @property
    def client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        return self._client

    def parse(self, extracted: dict, source_file: str = "") -> Optional[Order]:
        text = extracted.get("text", "")
        if not text.strip():
            return None

        prompt = (
            "Extraia as informações do pedido abaixo e retorne APENAS um JSON válido "
            "com este formato exato:\n"
            '{"header": {"order_number": "...", "issue_date": "DD/MM/YYYY", '
            '"customer_name": "..."}, '
            '"items": [{"description": "...", "quantity": 0.0}]}\n\n'
            f"Pedido:\n{text[:MAX_TEXT_CHARS]}"
        )

        try:
            message = self.client.messages.create(
                model=MODEL,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            content = message.content[0].text
            data = json.loads(content)
            header = OrderHeader(**data.get("header", {}))
            items = [OrderItem(**i) for i in data.get("items", [])]
            return Order(header=header, items=items, source_file=source_file)
        except Exception as e:
            logger.error(f"LLM fallback falhou [{source_file}]: {e}")
            return None
