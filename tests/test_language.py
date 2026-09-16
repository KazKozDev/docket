from docket.language import detect_language


def test_english_invoice():
    text = "Invoice for the services delivered in the period, payable to the vendor by the due date."
    assert detect_language(text)[0] == "en"


def test_spanish_invoice():
    text = "Factura por los servicios prestados en el periodo, a pagar por el cliente antes del vencimiento."
    assert detect_language(text)[0] == "es"


def test_confidence_reflects_how_one_sided_the_evidence_is():
    _, confident = detect_language("the and of to in for the and of to")
    assert confident == 1.0


def test_text_with_no_function_words_reports_unknown_rather_than_guessing():
    """The caller uses this to decide whether language-specific rules apply,
    so an honest "no idea" is worth more than a coin flip.
    """
    assert detect_language("BCN LHR IB3241 12C B24") == ("unknown", 0.0)


def test_empty_text():
    assert detect_language("") == ("unknown", 0.0)


def test_amounts_and_codes_do_not_confuse_the_detector():
    text = "Base imponible 1.234,56 IVA 21% 259,26 de la factura para el cliente"
    assert detect_language(text)[0] == "es"
