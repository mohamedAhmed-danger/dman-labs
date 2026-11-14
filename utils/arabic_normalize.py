import re


_DIACRITICS = re.compile(r"[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed]")
_LETTER_MAP = str.maketrans({
    "أ": "ا",
    "إ": "ا",
    "آ": "ا",
    "ة": "ه",
    "ى": "ي",
})


def normalize_arabic(value: str) -> str:
    """Normalize Arabic spelling variants for matching city names and aliases."""
    if not value:
        return ""
    return _DIACRITICS.sub("", value).translate(_LETTER_MAP).lower().strip()
