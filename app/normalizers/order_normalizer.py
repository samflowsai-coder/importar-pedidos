import re

from app.models.order import Order


class OrderNormalizer:
    def normalize(self, order: Order) -> Order:
        if order.header.order_number:
            order.header.order_number = order.header.order_number.strip().upper()
        if order.header.issue_date:
            order.header.issue_date = self._normalize_date(order.header.issue_date)
        if order.header.customer_name:
            order.header.customer_name = order.header.customer_name.strip().title()
        for item in order.items:
            if item.description:
                item.description = item.description.strip()
        return order

    def _normalize_date(self, date_str: str) -> str:
        cleaned = re.sub(r"[.\-]", "/", date_str.strip())
        parts = cleaned.split("/")
        if len(parts) == 3 and len(parts[2]) == 2:
            parts[2] = "20" + parts[2]
        return "/".join(parts)
