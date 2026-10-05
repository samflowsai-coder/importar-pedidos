from app.models.order import Order
from app.utils.logger import logger


class OrderValidator:
    def validate_detail(self, order: Order) -> list[str]:
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

        return errors

    def log_errors(self, order: Order, errors: list[str]) -> None:
        for e in errors:
            logger.warning(f"Validação [{order.source_file}]: {e}")

    def validate(self, order: Order) -> bool:
        errors = self.validate_detail(order)

        if errors:
            self.log_errors(order, errors)
            return False

        return True
