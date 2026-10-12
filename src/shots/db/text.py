"""Text helpers shared by the loaders (stored search keys) and the web filters (query terms)."""

from __future__ import annotations

import unicodedata


def fold(text: str) -> str:
    """Accent- and case-insensitive form for search."""
    return "".join(c for c in unicodedata.normalize("NFKD", str(text))
                   if not unicodedata.combining(c)).casefold().strip()


def search_key(home: str, away: str) -> str:
    """What the team search matches against: fold("home | away")."""
    return fold(f"{home} | {away}")
