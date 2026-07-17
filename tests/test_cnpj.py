from app.erp.cnpj import cnpj_digits


def test_cnpj_digits_strips_formatting():
    assert cnpj_digits("06.347.409/0296-51") == "06347409029651"


def test_cnpj_digits_already_clean_is_stable():
    assert cnpj_digits("06347409029651") == "06347409029651"


def test_cnpj_digits_none_and_empty():
    assert cnpj_digits(None) == ""
    assert cnpj_digits("   ") == ""


def test_cnpj_digits_drops_letters_and_spaces():
    assert cnpj_digits(" 06 347 409/0296-51 abc") == "06347409029651"
