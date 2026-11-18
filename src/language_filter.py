"""Conservative, deterministic filter for substantial Language name proposals.

Presentation changes (case, accents, punctuation, symbols, spacing and equivalent
artist-credit separators) are not useful. Never recommend changing a Latin-script
name to a newly introduced non-Latin spelling under the English-first policy.

This is a *review-list filter*, not a claim that a name is correct or permission
to change audio. Exact MusicBrainz identities and queued-job checks are separate.
"""
from __future__ import annotations

import re
import unicodedata


_CREDIT_SEPARATORS = re.compile(
    r"\s*(?:[&+,;×]|(?<!\w)(?:feat(?:uring)?|ft|and|with|starring)\.?(?!\w)|(?<=\s)x(?=\s))\s*",
    re.IGNORECASE,
)


def _letters_and_numbers(value: str) -> str:
    """Casefold and remove Unicode presentation differences, not actual words."""
    value = str(value or "").casefold().replace("×", "x")
    value = re.sub(r"\bpt\.?\s*(?=\d)", "part", value)
    value = unicodedata.normalize("NFKD", value)
    return "".join(char for char in value
                   if unicodedata.category(char)[0] in {"L", "N"})


def _artist_credits(value: str) -> list[str]:
    """Compare the same billed people despite feat./&/comma/order variations."""
    return sorted(filter(None, (_letters_and_numbers(part)
                                for part in _CREDIT_SEPARATORS.split(value or ""))))


def _gains_non_latin(before: str, after: str) -> bool:
    def non_latin(value):
        return any(char.isalpha() and "LATIN" not in unicodedata.name(char, "")
                   for char in value)
    # With English-first naming, an existing Latin-only name should not be
    # replaced with newly introduced CJK, Arabic, Cyrillic, etc. by default.
    return not non_latin(before) and non_latin(after)


def _substantive(field: str, before: str, after: str) -> bool:
    if _letters_and_numbers(before) == _letters_and_numbers(after):
        return False
    if field.upper().replace(" ", "") in {"ARTIST", "ALBUMARTIST"}:
        if _artist_credits(before) == _artist_credits(after):
            return False
    if _gains_non_latin(before, after):
        return False
    return True


def meaningful_name_change(row: dict) -> bool:
    """True when at least one *visible* field has a non-cosmetic difference.

    New reviews carry field-level comparisons. Earlier saved reviews carry
    only before/after labels, which are supported without another audio scan.
    """
    diffs = row.get("display_diffs")
    if diffs:
        return any(_substantive(str(diff.get("field", "")),
                                str(diff.get("before", "")),
                                str(diff.get("after", "")))
                   for diff in diffs)
    old, new = str(row.get("before_label") or ""), str(row.get("after_label") or "")
    field = str(row.get("changed_field") or "")
    if field == "Title / artist" and " - " in old and " - " in new:
        old_artist, old_title = old.split(" - ", 1)
        new_artist, new_title = new.split(" - ", 1)
        return (_substantive("TITLE", old_title, new_title)
                or _substantive("ARTIST", old_artist, new_artist))
    return _substantive(field, old, new)
