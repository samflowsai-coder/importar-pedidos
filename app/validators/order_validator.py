from app.models.order import Order
from app.utils.logger import logger


class OrderValidator:
    def validate(self, order: Order) -> bool:
        errors = []

        if not order.header.order_number:
            errors.append("Número do pedido não encontrado")

        if not order.items:
            errors.append("Nenhum item encontrado no pedido")

        for i, item in enumerate(order.items):
            if not item.description:
                errors.append(f"Item {i + 1}: descrição ausente")
            if item.quantity is None or item.quantity <= 0:
                errors.append(f"Item {i + 1}: quantidade inválida ({item.quantity})")

        if errors:
            for e in errors:
                logger.warning(f"Validação [{order.source_file}]: {e}")
            return False

        return True
