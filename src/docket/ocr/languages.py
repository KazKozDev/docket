"""ISO 639-1 language codes → each engine's own names.

Docket configuration speaks ISO 639-1 (`DOCKET_OCR_LANGUAGES=en,de`); each
backend maps to what its engine expects. A code with no mapping is an error
at configuration time, not a silent fallback to English.
"""
from __future__ import annotations

TESSERACT = {
    "bg": "bul", "cs": "ces", "da": "dan", "de": "deu", "el": "ell", "en": "eng",
    "es": "spa", "et": "est", "fi": "fin", "fr": "fra", "ga": "gle", "hr": "hrv",
    "hu": "hun", "it": "ita", "lt": "lit", "lv": "lav", "mt": "mlt", "nl": "nld",
    "no": "nor", "pl": "pol", "pt": "por", "ro": "ron", "ru": "rus", "sk": "slk",
    "sl": "slv", "sv": "swe", "tr": "tur", "uk": "ukr", "ar": "ara", "zh": "chi_sim",
    "ja": "jpn", "ko": "kor",
}


class UnknownLanguage(ValueError):
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


__all__ = ["TESSERACT", "UnknownLanguage", "parse_languages", "tesseract_codes"]
