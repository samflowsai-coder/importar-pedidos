class FirebirdError(Exception):
    """Base for all ERP/Firebird layer errors."""


class FirebirdConnectionError(FirebirdError):
    """Cannot connect to or open the .fdb file."""


class FirebirdOrderAlreadyExistsError(FirebirdError):
    """Order number already exists in DB (idempotency guard)."""

    def __init__(self, order_number: str) -> None:
        super().__init__(f"Pedido {order_number!r} já existe no banco.")
        self.order_number = order_number


class FirebirdProductNotFoundError(FirebirdError):
    """product_code referenced in an item does not exist in ERP catalog."""

    def __init__(self, product_code: str) -> None:
        super().__init__(f"Produto {product_code!r} não encontrado no ERP.")
        self.product_code = product_code


class FirebirdMappingError(FirebirdError):
    """Cannot map Order fields to ERP table columns (schema mismatch)."""


class FirebirdClientNotFoundError(FirebirdError):
    """Client CNPJ not found in CADASTRO — cannot insert order without CLIENTE FK."""

    def __init__(self, cnpj: str, customer_name: str | None = None) -> None:
        name = f" ({customer_name})" if customer_name else ""
        super().__init__(f"Cliente CNPJ {cnpj!r}{name} não encontrado em CADASTRO.")
        self.cnpj = cnpj
        self.customer_name = customer_name
