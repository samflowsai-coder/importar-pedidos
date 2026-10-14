from app.models.order import Order, OrderHeader, OrderItem
from app.utils.logger import logger
from app.validators.order_validator import OrderValidator


def _order(numero="123", itens=None, source="x.xlsx"):
    if itens is None:
        itens = [OrderItem(description="Tenis", quantity=2)]
    return Order(header=OrderHeader(order_number=numero), items=itens, source_file=source)


def test_detail_pedido_valido_devolve_lista_vazia():
    assert OrderValidator().validate_detail(_order()) == []


def test_detail_devolve_mensagens_atuais():
    o = _order(numero="", itens=[OrderItem(description="", quantity=0)])
    assert OrderValidator().validate_detail(o) == [
        "Número do pedido não encontrado",
        "Item 1: descrição ausente",
        "Item 1: quantidade inválida (0.0)",
    ]


def test_detail_sem_itens():
    assert OrderValidator().validate_detail(_order(itens=[])) == ["Nenhum item encontrado no pedido"]


def test_validate_mantem_retorno_bool():
    v = OrderValidator()
    assert v.validate(_order()) is True
    assert v.validate(_order(numero="")) is False


def test_validate_mantem_logs():
    msgs = []
    hid = logger.add(lambda m: msgs.append(m.record["message"]), level="WARNING")
    try:
        OrderValidator().validate(_order(numero="", source="arq.xlsx"))
    finally:
        logger.remove(hid)
    assert msgs == ["Validação [arq.xlsx]: Número do pedido não encontrado"]


def test_detail_nao_loga():
    msgs = []
    hid = logger.add(lambda m: msgs.append(m.record["message"]), level="WARNING")
    try:
        OrderValidator().validate_detail(_order(numero=""))
    finally:
        logger.remove(hid)
    assert msgs == []
