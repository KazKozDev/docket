"""Real checksum algorithms for IBAN and EU-style VAT numbers.

This is deliberately not "does it look like an IBAN" — that's what
`_TAX_ID_RE` in validate.py already does for the generic tax-id field. This
module implements the actual arithmetic so a transposed digit gets caught
even when the format is otherwise plausible, per the job spec's "IBAN and
VAT numbers have matching checksums" requirement.

Only a handful of VAT checksum algorithms are implemented (the ones that
are simple, well-documented, and common in test data: DE, NL, GB). For any
other recognized-looking VAT number, `validate_vat` returns None — "can't
verify the checksum, don't claim it's wrong" — rather than silently
accepting or rejecting a country it doesn't know.
"""
from __future__ import annotations

import re

_IBAN_RE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$")
_IBAN_COUNTRY_LENGTHS: dict[str, int] = {
    # Western / Central / Northern Europe
    "AT": 20,
    "BE": 16,
    "CH": 21,
    "DE": 22,
    "DK": 18,
    "ES": 24,
    "FI": 18,
    "FO": 18,
    "FR": 27,
    "GB": 22,
    "GI": 23,
    "GL": 18,
    "IE": 22,
    "IS": 26,
    "IT": 27,
    "LI": 21,
    "LU": 20,
    "MC": 27,
    "NL": 18,
    "NO": 15,
    "PT": 25,
    "SE": 24,
    "SM": 27,
    "VA": 22,
    # Southern / Eastern / Southeastern Europe
    "AL": 28,
    "AD": 24,
    "BA": 20,
    "BG": 22,
    "BY": 28,
    "CY": 28,
    "CZ": 24,
    "EE": 20,
    "GR": 27,
    "HR": 21,
    "HU": 28,
    "LT": 20,
    "LV": 21,
    "MD": 24,
    "ME": 22,
    "MK": 19,
    "MT": 31,
    "PL": 28,
    "RO": 24,
    "RS": 22,
    "RU": 33,
    "SI": 19,
    "SK": 24,
    "TR": 26,
    "UA": 29,
    "XK": 20,
    # Americas
    "BR": 29,
}
_NON_IBAN_COUNTRIES = frozenset({"US", "CA"})


def is_iban_shaped(value: str) -> bool:
    """Whether a string could be an IBAN at all, before asking if it's valid.

    "Fails the mod-97 checksum" says a real IBAN carries a bad check digit,
    and sends a reviewer to compare digits. A string that was never an IBAN
    — a template's "[IBAN code]", a bank's name, an empty field — is a
    different finding with a different remedy: ask the vendor for the
    number. The two are worth separating because they route differently.
    """
    return bool(_IBAN_RE.match(value.replace(" ", "").upper()))


def validate_iban(iban: str) -> bool:
    """ISO 7064 MOD 97-10, the actual IBAN check-digit algorithm.

    Move the first 4 characters to the end, map letters to numbers
    (A=10 .. Z=35), and the whole thing must be congruent to 1 mod 97.
    """
    iban = iban.replace(" ", "").upper()
    if not _IBAN_RE.match(iban):
        return False
    country = iban[:2]
    if country in _NON_IBAN_COUNTRIES:
        return False
    expected_len = _IBAN_COUNTRY_LENGTHS.get(country)
    if expected_len is not None and len(iban) != expected_len:
        return False

    rearranged = iban[4:] + iban[:4]
    try:
        numeric = "".join(str(int(ch, 36)) for ch in rearranged)
    except ValueError:
        return False
    return int(numeric) % 97 == 1


def _de_vat_checksum(digits: str) -> bool:
    """German USt-IdNr: ISO 7064 MOD 11-10 over 9 digits + 1 check digit."""
    if len(digits) != 9:
        return False
    product = 10
    for d in digits[:-1]:
        s = (int(d) + product) % 10
        if s == 0:
            s = 10
        product = (2 * s) % 11
    check_digit = 11 - product
    if check_digit == 10:
        check_digit = 0
    return check_digit == int(digits[-1])


_NL_VAT_RE = re.compile(r"^(\d{9})B\d{2}$")


def _nl_vat_checksum(body: str) -> bool:
    """Dutch BTW-nummer: nine digits carrying the check, then a mandatory
    "B" and a two-digit sub-number (NL817576320B01).

    The suffix is part of the number, not decoration — an earlier version
    accepted only the nine digits and so rejected every real Dutch VAT
    number it was shown, including the valid one printed on the invoice
    that exposed this.
    """
    match = _NL_VAT_RE.match(body)
    digits = match.group(1) if match else body
    if len(digits) != 9 or not digits.isdigit():
        return False
    total = sum(int(d) * w for d, w in zip(digits[:8], range(9, 1, -1)))
    check = total % 11
    if check == 10:
        check = 0
    return check == int(digits[8])


def _gb_vat_checksum(digits: str) -> bool:
    """UK VAT: 9 digits, weights 8..2 on the first 7, standard + 55-offset ranges."""
    if len(digits) != 9:
        return False
    weights = range(8, 1, -1)
    total = sum(int(d) * w for d, w in zip(digits[:7], weights))
    check = int(digits[7:9])
    total += check
    return total % 97 == 0 or (total - 55) % 97 == 0


_ES_DNI_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"
_ES_CIF_CONTROL_LETTERS = "JABCDEFGHI"
# Company forms whose control character is always a letter (foreign entities,
# non-profits and the like); N/P/Q/R/S/W plus the personal-style X/Y/Z NIEs.
_ES_LETTER_CONTROL = set("PQRSNW")
_ES_DIGIT_CONTROL = set("ABEH")


def _es_vat_checksum(body: str) -> bool:
    """Spanish NIF/CIF/NIE. Three shapes share one field, so all three are
    implemented — a Barcelona invoice can carry any of them.
    """
    if len(body) != 9:
        return False
    first, middle, last = body[0], body[1:8], body[8]

    # NIE: X/Y/Z stand in for a leading 0/1/2, then it's a DNI.
    if first in "XYZ" and middle.isdigit():
        number = int(str("XYZ".index(first)) + middle)
        return last == _ES_DNI_LETTERS[number % 23]

    # DNI: eight digits plus a check letter.
    if body[:8].isdigit():
        return last == _ES_DNI_LETTERS[int(body[:8]) % 23]

    # CIF: an organisation letter, seven digits, then a digit or letter.
    if not middle.isdigit():
        return False
    odd_sum = 0
    for digit in middle[0::2]:  # positions 1,3,5,7 (1-indexed)
        doubled = int(digit) * 2
        odd_sum += doubled // 10 + doubled % 10
    even_sum = sum(int(d) for d in middle[1::2])
    control = (10 - (odd_sum + even_sum) % 10) % 10

    if first in _ES_LETTER_CONTROL:
        return last == _ES_CIF_CONTROL_LETTERS[control]
    if first in _ES_DIGIT_CONTROL:
        return last == str(control)
    return last == str(control) or last == _ES_CIF_CONTROL_LETTERS[control]


def _ie_vat_checksum(body: str) -> bool:
    """Irish VAT: seven digits, a check letter, and optionally a second
    letter that participates in the sum (the post-2013 format).
    """
    letters = "WABCDEFGHIJKLMNOPQRSTUV"
    if len(body) == 8 and body[:7].isdigit() and body[7].isalpha():
        total = sum(int(d) * w for d, w in zip(body[:7], range(8, 1, -1)))
        return body[7] == letters[total % 23]
    if len(body) == 9 and body[:7].isdigit() and body[7:].isalpha():
        total = sum(int(d) * w for d, w in zip(body[:7], range(8, 1, -1)))
        total += (letters.index(body[8]) if body[8] in letters else 0) * 9
        return body[7] == letters[total % 23]
    return False


def _fi_vat_checksum(digits: str) -> bool:
    """Finnish ALV-numero: 8 digits, weights 7-9-10-5-8-4-2, mod 11."""
    if len(digits) != 8 or not digits.isdigit():
        return False
    total = sum(int(d) * w for d, w in zip(digits[:7], (7, 9, 10, 5, 8, 4, 2)))
    remainder = total % 11
    if remainder == 1:
        return False  # this combination is never issued
    check = 0 if remainder == 0 else 11 - remainder
    return check == int(digits[7])


def _at_vat_checksum(body: str) -> bool:
    if not re.fullmatch(r"U\d{8}", body):
        return False
    digits = body[1:]
    total = 0
    for digit, weight in zip(digits[:7], (1, 2, 1, 2, 1, 2, 1)):
        product = int(digit) * weight
        total += product // 10 + product % 10
    return (10 - (total + 4) % 10) % 10 == int(digits[-1])


def _fr_vat_checksum(body: str) -> bool:
    if not re.fullmatch(r"\d{11}", body):
        return False
    return int(body[:2]) == (12 + 3 * (int(body[2:]) % 97)) % 97


def _it_vat_checksum(digits: str) -> bool:
    if not re.fullmatch(r"\d{11}", digits):
        return False
    total = sum(int(digits[index]) for index in range(0, 10, 2))
    for index in range(1, 10, 2):
        doubled = int(digits[index]) * 2
        total += doubled - 9 if doubled > 9 else doubled
    return (10 - total % 10) % 10 == int(digits[-1])


def _pl_vat_checksum(digits: str) -> bool:
    if not re.fullmatch(r"\d{10}", digits):
        return False
    check = sum(int(d) * w for d, w in zip(digits[:9], (6, 5, 7, 2, 3, 4, 5, 6, 7))) % 11
    return check != 10 and check == int(digits[-1])


def _luhn_valid(digits: str) -> bool:
    if not digits.isdigit():
        return False
    total = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        value = int(digit)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _se_vat_checksum(digits: str) -> bool:
    return bool(re.fullmatch(r"\d{12}", digits)) and digits[-2:] == "01" and _luhn_valid(digits[:10])


def _ch_vat_checksum(body: str) -> bool:
    match = re.fullmatch(r"(\d{9})(?:MWST|TVA|IVA)?", body)
    if not match:
        return False
    digits = match.group(1)
    remainder = sum(int(d) * w for d, w in zip(digits[:8], (5, 4, 3, 2, 7, 6, 5, 4))) % 11
    check = 11 - remainder
    if check == 11:
        check = 0
    if check == 10:
        return False
    return check == int(digits[-1])


def _no_vat_checksum(body: str) -> bool:
    match = re.fullmatch(r"(\d{9})(?:MVA)?", body)
    if not match:
        return False
    digits = match.group(1)
    check = 11 - sum(int(d) * w for d, w in zip(digits[:8], (3, 2, 7, 6, 5, 4, 3, 2))) % 11
    if check == 11:
        check = 0
    if check == 10:
        return False
    return check == int(digits[-1])


_VAT_CHECKERS = {
    "AT": _at_vat_checksum,
    "FR": _fr_vat_checksum,
    "IT": _it_vat_checksum,
    "PL": _pl_vat_checksum,
    "SE": _se_vat_checksum,
    "CHE": _ch_vat_checksum,
    "NO": _no_vat_checksum,
    "DE": _de_vat_checksum,
    "NL": _nl_vat_checksum,
    "GB": _gb_vat_checksum,
    "ES": _es_vat_checksum,
    "IE": _ie_vat_checksum,
    "FI": _fi_vat_checksum,
}
_VAT_RE = re.compile(r"^(CHE|[A-Z]{2})([0-9A-Z]{2,14})$")


# VAT number bodies per country (after the country prefix), from the formats
# the EU VIES service documents, plus GB/XI, CH and NO.
_VAT_FORMATS = {
    "AT": r"U\d{8}", "BE": r"[01]\d{9}", "BG": r"\d{9,10}", "CY": r"\d{8}[A-Z]",
    "CZ": r"\d{8,10}", "DE": r"\d{9}", "DK": r"\d{8}", "EE": r"\d{9}", "EL": r"\d{9}",
    "ES": r"[0-9A-Z]\d{7}[0-9A-Z]", "FI": r"\d{8}", "FR": r"[0-9A-HJ-NP-Z]{2}\d{9}", "HR": r"\d{11}",
    "HU": r"\d{8}", "IE": r"\d{7}[A-W][A-I]?|\d[A-Z+*]\d{5}[A-W]", "IT": r"\d{11}", "LT": r"\d{9}|\d{12}",
    "LU": r"\d{8}", "LV": r"\d{11}", "MT": r"\d{8}", "NL": r"\d{9}B\d{2}", "PL": r"\d{10}",
    "PT": r"\d{9}", "RO": r"\d{2,10}", "SE": r"\d{12}", "SI": r"\d{8}", "SK": r"\d{10}",
    "GB": r"\d{9}|\d{12}|GD\d{3}|HA\d{3}", "XI": r"\d{9}|\d{12}|GD\d{3}|HA\d{3}",
    "CHE": r"\d{9}(?:MWST|TVA|IVA)?", "NO": r"\d{9}(?:MVA)?",
}


def vat_format_ok(value: str) -> bool | None:
    """Whether a VAT number has its country's format: True / False for a
    country with a known format, None for any other prefix."""
    match = _VAT_RE.match(value.replace(" ", "").replace("-", "").replace(".", "").upper())
    if not match:
        return False
    fmt = _VAT_FORMATS.get(match.group(1))
    if fmt is None:
        return None
    return re.fullmatch(fmt, match.group(2)) is not None


def is_vat_shaped(value: str) -> bool:
    """Whether a string could be a VAT number at all — see is_iban_shaped."""
    return bool(
        _VAT_RE.match(value.replace(" ", "").replace("-", "").replace(".", "").upper())
    )


def validate_vat(vat: str) -> bool | None:
    """Returns True/False if the checksum could be verified, None if this
    country's algorithm isn't implemented (format looks plausible, but
    correctness is unknown rather than assumed).
    """
    clean = vat.replace(" ", "").replace("-", "").replace(".", "").upper()
    match = _VAT_RE.match(clean)
    if not match:
        return False

    country, body = match.groups()
    checker = _VAT_CHECKERS.get(country)
    if checker is not None:
        return checker(body)

    return None


def validate_us_ein(ein: str) -> bool:
    """Validate US Employer Identification Number (EIN)."""
    match = re.fullmatch(r"(\d{2})-?(\d{7})", ein.strip())
    if not match:
        return False
    valid_prefixes = {
        *range(1, 7), *range(10, 17), *range(20, 28), *range(30, 40),
        *range(40, 49), *range(50, 60), *range(60, 69), *range(71, 78),
        *range(80, 89), *range(90, 100),
    }
    return int(match.group(1)) in valid_prefixes


def validate_ca_bn(bn: str) -> bool:
    """Validate Canadian Business Number (BN / GST / HST)."""
    clean = re.sub(r"\s", "", bn).upper()
    match = re.fullmatch(r"(\d{9})(?:[A-Z]{2}\d{4})?", clean)
    return bool(match and _luhn_valid(match.group(1)))


def validate_br_cnpj(cnpj: str) -> bool:
    """Validate Brazilian CNPJ company tax number (Cadastro Nacional da Pessoa Jurídica)."""
    digits = re.sub(r"\D", "", cnpj)
    if len(digits) != 14 or len(set(digits)) == 1:
        return False

    def check_digit(prefix: str, weights: tuple[int, ...]) -> str:
        remainder = sum(int(d) * w for d, w in zip(prefix, weights)) % 11
        return str(0 if remainder < 2 else 11 - remainder)

    first = check_digit(digits[:12], (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2))
    second = check_digit(digits[:12] + first, (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2))
    return digits[-2:] == first + second


def validate_br_cpf(cpf: str) -> bool:
    """Validate Brazilian CPF individual tax number (Cadastro de Pessoas Físicas)."""
    digits = re.sub(r"\D", "", cpf)
    if len(digits) != 11 or len(set(digits)) == 1:
        return False

    def check_digit(prefix: str, start: int) -> str:
        remainder = sum(int(d) * w for d, w in zip(prefix, range(start, 1, -1))) % 11
        return str(0 if remainder < 2 else 11 - remainder)

    first = check_digit(digits[:9], 10)
    second = check_digit(digits[:9] + first, 11)
    return digits[-2:] == first + second


def validate_tax_id(
    tax_id: str,
    country_hint: str | None = None,
) -> tuple[bool | None, str | None]:
    """Validate tax ID against national rules for US, Canada, and Brazil.

    Args:
        tax_id: Raw or formatted tax identifier string.
        country_hint: Optional ISO country code hint ('US', 'CA', 'BR').

    Returns:
        A tuple of (is_valid, scheme_name). If the identifier is recognized
        as a national scheme (US EIN, Canadian BN, Brazilian CNPJ or CPF),
        is_valid is True or False and scheme_name is the human-readable scheme.
        If the identifier cannot be unambiguously attributed to one of these
        national schemes, (None, None) is returned.
    """
    cleaned = tax_id.strip()
    hint = country_hint.upper() if country_hint else None

    # US EIN: typically XX-XXXXXXX or 9 digits if hint is US
    if (
        hint == "US"
        and (re.match(r"^\d{2}-\d{7}$", cleaned) or re.match(r"^\d{9}$", cleaned))
    ) or (bool(re.match(r"^\d{2}-\d{7}$", cleaned))):
        return validate_us_ein(cleaned), "US EIN"

    # Canadian Business Number: 9 digits + optional program code (e.g. RT 0001)
    if (
        hint == "CA"
        and (
            re.match(r"^\d{9}$", cleaned)
            or re.match(r"^\d{9}\s*[A-Z]{2}\s*\d{4}$", cleaned, re.IGNORECASE)
        )
    ) or (bool(re.match(r"^\d{9}\s*[A-Z]{2}\s*\d{4}$", cleaned, re.IGNORECASE))):
        return validate_ca_bn(cleaned), "Canadian BN"

    # Brazilian CNPJ / CPF:
    if hint == "BR":
        digits = re.sub(r"\D", "", cleaned)
        if len(digits) == 14:
            return validate_br_cnpj(cleaned), "Brazilian CNPJ"
        if len(digits) == 11:
            return validate_br_cpf(cleaned), "Brazilian CPF"

    # Formatted Brazilian CNPJ (XX.XXX.XXX/XXXX-XX)
    if re.match(r"^\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}$", cleaned):
        return validate_br_cnpj(cleaned), "Brazilian CNPJ"

    # Formatted Brazilian CPF (XXX.XXX.XXX-XX)
    if re.match(r"^\d{3}\.\d{3}\.\d{3}-\d{2}$", cleaned):
        return validate_br_cpf(cleaned), "Brazilian CPF"

    return None, None
