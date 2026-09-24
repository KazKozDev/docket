import pytest

from docket.checksums import validate_iban, validate_vat


def test_valid_german_iban():
    assert validate_iban("DE89370400440532013000") is True


def test_iban_with_spaces_and_lowercase():
    assert validate_iban("de89 3704 0044 0532 0130 00") is True


def test_transposed_digit_fails_iban_checksum():
    # Two digits swapped relative to the valid IBAN above.
    assert validate_iban("DE89370400440532013100") is False


def test_wrong_length_for_country_fails():
    assert validate_iban("DE8937040044053201300") is False  # one digit short


def test_garbage_is_not_an_iban():
    assert validate_iban("not-an-iban") is False


def test_valid_german_vat():
    assert validate_vat("DE136695976") is True


def test_invalid_german_vat_checksum():
    assert validate_vat("DE136695975") is False


def test_valid_dutch_vat():
    assert validate_vat("NL123456782") is True


def test_unimplemented_country_returns_none_not_false():
    # Format looks plausible; we just don't have this country's algorithm.
    assert validate_vat("JP12345678901") is None


def test_garbage_vat_is_false():
    assert validate_vat("12345") is False


# Spain is the client's home country for this pipeline's target deployment,
# and Ireland is one of the group's carriers — both were unsupported until
# these were added. The positives below are the real, published VAT numbers
# of listed companies, so the algorithms are checked against reality rather
# than against themselves.
def test_real_spanish_company_cifs():
    assert validate_vat("ESA28015865") is True  # Telefónica
    assert validate_vat("ESA39000013") is True  # Banco Santander
    assert validate_vat("ESA15075062") is True  # Inditex


def test_spanish_cif_with_a_broken_digit():
    assert validate_vat("ESA28015866") is False


def test_spanish_dni_and_nie_forms():
    assert validate_vat("ES12345678Z") is True  # DNI
    assert validate_vat("ESX1234567L") is True  # NIE
    assert validate_vat("ES12345678A") is False


def test_real_irish_vat():
    assert validate_vat("IE6388047V") is True  # Google Ireland


def test_irish_vat_with_a_broken_check_letter():
    assert validate_vat("IE6388047X") is False


def test_real_finnish_vat():
    # Added after a real Finnish invoice came through the pipeline carrying
    # a VAT number the checker could only shrug at.
    assert validate_vat("FI20774740") is True  # Nokia
    assert validate_vat("FI09461356") is True  # KONE


def test_finnish_vat_too_short_is_rejected():
    # The sample invoice's own "FI1234567" is seven digits; Finland issues
    # eight. A malformed number on a real invoice is worth a human's glance.
    assert validate_vat("FI1234567") is False


def test_real_dutch_vat_carries_a_b_suffix():
    """NL817576320B01 is the published VAT number of a real company, and the
    "B01" sub-number is part of the format, not decoration. An earlier
    version checked only the nine digits and so rejected every genuine Dutch
    VAT number it was shown.
    """
    assert validate_vat("NL817576320B01") is True
    assert validate_vat("NL123456782B01") is True


def test_dutch_vat_with_a_broken_check_digit_still_fails():
    assert validate_vat("NL817576321B01") is False


def test_malformed_dutch_suffix_is_rejected():
    assert validate_vat("NL817576320B0") is False


def test_shape_is_distinguishable_from_validity():
    """Two findings with two remedies: a bad check digit means compare the
    digits, a string that was never an IBAN means ask the vendor for one.
    """
    from docket.checksums import is_iban_shaped, is_vat_shaped

    assert is_iban_shaped("DE89370400440532013100") is True  # shaped, bad checksum
    assert validate_iban("DE89370400440532013100") is False
    assert is_iban_shaped("[IBAN code]") is False  # never an IBAN
    assert is_vat_shaped("NL817576320B01") is True
    assert is_vat_shaped("[VAT id]") is False


def test_european_vats():
    # France (Michelin)
    assert validate_vat("FR40303265045") is True
    assert validate_vat("FR40303265046") is False

    # Italy (Pirelli)
    assert validate_vat("IT00743110157") is True
    assert validate_vat("IT00743110158") is False

    # Poland (PKN Orlen)
    assert validate_vat("PL5260001246") is True
    assert validate_vat("PL5260001247") is False

    # Sweden
    assert validate_vat("SE556012579001") is True
    assert validate_vat("SE556012579002") is False

    # Austria
    assert validate_vat("ATU13585627") is True
    assert validate_vat("ATU13585628") is False

    # Switzerland (MWST)
    assert validate_vat("CHE-100.000.006 MWST") is True
    assert validate_vat("CHE-100.000.007 MWST") is False

    # Norway (MVA)
    assert validate_vat("NO999999999MVA") is True
    assert validate_vat("NO999999998MVA") is False


def test_brazil_iban():
    # Valid 29-character Brazilian IBAN
    valid_br = "BR1200000000000000000000001C1"
    assert validate_iban(valid_br) is True

    # Bad check digit
    assert validate_iban("BR1300000000000000000000001C1") is False

    # Wrong length
    assert validate_iban("BR1200000000000000000000001C") is False


def test_non_iban_countries():
    # US and Canada do not use IBAN
    assert validate_iban("US12345678901234567890") is False
    assert validate_iban("CA12345678901234567890") is False


def test_us_ein():
    from docket.checksums import validate_tax_id, validate_us_ein

    assert validate_us_ein("12-3456789") is True
    assert validate_us_ein("00-3456789") is False

    # Formatted EIN recognition
    valid_res, scheme = validate_tax_id("12-3456789")
    assert valid_res is True
    assert scheme == "US EIN"

    invalid_res, scheme = validate_tax_id("00-3456789")
    assert invalid_res is False
    assert scheme == "US EIN"


def test_canada_bn():
    from docket.checksums import validate_ca_bn, validate_tax_id

    assert validate_ca_bn("123456782RT0001") is True
    assert validate_ca_bn("123456783RT0001") is False

    valid_res, scheme = validate_tax_id("123456782 RT 0001")
    assert valid_res is True
    assert scheme == "Canadian BN"

    invalid_res, scheme = validate_tax_id("123456783 RT 0001")
    assert invalid_res is False
    assert scheme == "Canadian BN"


def test_brazil_cnpj_and_cpf():
    from docket.checksums import validate_br_cnpj, validate_br_cpf, validate_tax_id

    # CNPJ
    assert validate_br_cnpj("11.222.333/0001-81") is True
    assert validate_br_cnpj("11.222.333/0001-82") is False

    valid_cnpj, scheme = validate_tax_id("11.222.333/0001-81")
    assert valid_cnpj is True
    assert scheme == "Brazilian CNPJ"

    invalid_cnpj, scheme = validate_tax_id("11.222.333/0001-82")
    assert invalid_cnpj is False
    assert scheme == "Brazilian CNPJ"

    # CPF
    assert validate_br_cpf("123.456.789-09") is True
    assert validate_br_cpf("123.456.789-00") is False

    valid_cpf, scheme = validate_tax_id("123.456.789-09")
    assert valid_cpf is True
    assert scheme == "Brazilian CPF"

    invalid_cpf, scheme = validate_tax_id("123.456.789-00")
    assert invalid_cpf is False
    assert scheme == "Brazilian CPF"


@pytest.mark.parametrize(
    "value, ok",
    [
        ("GB123456789", True), ("GB123456789012", True), ("GBGD123", True), ("GB-771-4402", False),
        ("DE136695976", True), ("DE13669597", False), ("ATU12345678", True), ("AT12345678", False),
        ("NL123456789B01", True), ("FR40303265045", True), ("CHE-123.456.789 MWST", True),
        ("US-77-4412200", None), ("not a number", False),
    ],
)
def test_vat_formats_follow_the_country(value, ok):
    from docket.checksums import vat_format_ok

    assert vat_format_ok(value) is ok
