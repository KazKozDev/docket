"""ISO 639-1 language codes → each engine's own names.

Docket configuration speaks ISO 639-1 (`DOCKET_OCR_LANGUAGES=en,de`); each
backend maps to what its engine expects. A code with no mapping is an error
at configuration time, not a silent fallback to English.
"""
from __future__ import annotations

from ..errors import ConfigurationError

TESSERACT = {
    "bg": "bul", "cs": "ces", "da": "dan", "de": "deu", "el": "ell", "en": "eng",
    "es": "spa", "et": "est", "fi": "fin", "fr": "fra", "ga": "gle", "hr": "hrv",
    "hu": "hun", "it": "ita", "lt": "lit", "lv": "lav", "mt": "mlt", "nl": "nld",
    "no": "nor", "pl": "pol", "pt": "por", "ro": "ron", "ru": "rus", "sk": "slk",
    "sl": "slv", "sv": "swe", "tr": "tur", "uk": "ukr", "ar": "ara", "zh": "chi_sim",
    "ja": "jpn", "ko": "kor",
}


class UnknownLanguage(ConfigurationError):
    pass


def parse_languages(value: str | list[str]) -> list[str]:
    """'en,de' or ['en', 'de'] → ['en', 'de'], validated as known ISO 639-1 codes."""
    items = value.replace("+", ",").split(",") if isinstance(value, str) else list(value)
    codes = [c.strip().lower() for c in items if c and c.strip()]
    if not codes:
        raise UnknownLanguage("at least one OCR language is required")
    unknown = [c for c in codes if c not in TESSERACT]
    if unknown:
        raise UnknownLanguage(
            f"unknown OCR language code(s) {unknown}; use ISO 639-1 codes such as "
            f"{', '.join(sorted(TESSERACT)[:8])}, ..."
        )
    return codes


def tesseract_codes(languages: list[str]) -> list[str]:
    return [TESSERACT[c] for c in languages]


# PaddleOCR recognizes one script family per model. Each family maps to the
# code PaddleOCR's own model selection takes (`lang=`) and to the mobile
# recognition model docket loads for it.
_PADDLE_LATIN = {
    "cs", "da", "de", "en", "es", "et", "fi", "fr", "ga", "hr", "hu", "it", "lt",
    "lv", "mt", "nl", "no", "pl", "pt", "ro", "sk", "sl", "sv", "tr",
}
_PADDLE_FAMILIES: dict[str, tuple[set[str], str]] = {
    # family: (ISO codes, mobile recognition model)
    "latin": (_PADDLE_LATIN, "latin_PP-OCRv5_mobile_rec"),
    "eslav": ({"ru", "uk"}, "eslav_PP-OCRv5_mobile_rec"),
    "cyrillic": ({"bg"}, "cyrillic_PP-OCRv5_mobile_rec"),
    "el": ({"el"}, "el_PP-OCRv5_mobile_rec"),
    "arabic": ({"ar"}, "arabic_PP-OCRv5_mobile_rec"),
    "korean": ({"ko"}, "korean_PP-OCRv5_mobile_rec"),
    "cjk": ({"zh", "ja"}, "PP-OCRv5_mobile_rec"),
}
_PADDLE_LANG_CODE = {"zh": "ch", "ja": "japan", "ko": "korean"}


def paddle_language(languages: list[str]) -> tuple[str, str]:
    """Pick the PaddleOCR recognition language for a set of ISO codes.

    English alone gets the dedicated English model. Several languages must
    share one script family (e.g. en+de+fr → Latin); mixing families (en+ru)
    needs one backend per family and is refused rather than half-read.
    """
    codes = set(languages)
    if codes == {"en"}:
        return "en", "en_PP-OCRv5_mobile_rec"
    for family, (members, model) in _PADDLE_FAMILIES.items():
        if codes <= members:
            primary = next((c for c in languages if c != "en"), languages[0])
            return _PADDLE_LANG_CODE.get(primary, primary), model
    raise UnknownLanguage(
        f"PaddleOCR reads one script family per model; {sorted(codes)} mixes families "
        f"or includes a language it has no model for"
    )


__all__ = ["TESSERACT", "UnknownLanguage", "paddle_language", "parse_languages", "tesseract_codes"]
