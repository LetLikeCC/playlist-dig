"""String normalisation used for identity, dedupe, version detection and diff.

Kept deliberately crude and documented: every rule here is a guess about what
"the same song" means, so it lives in one place and is unit-tested.
"""

from __future__ import annotations

import re
import unicodedata

#: trailing/leading noise that platforms add to the same underlying song
_NOISE = [
    (re.compile(r"\s*[\(\（\[\【](?:feat|ft|with)\.?[^\)\）\]\】]*[\)\）\]\】]", re.I), ""),
    (re.compile(r"\s*[\(\（\[\【](?:live|remix|remaster(?:ed)?|version|ver\.?|demo|acoustic|"
                r"instrumental|off\s*vocal|伴奏|现场|翻自|纯音乐|重制|重混)[^\)\）\]\】]*[\)\）\]\】]", re.I), ""),
    (re.compile(r"\s*[-–—]\s*(?:live|remix|remaster(?:ed)?|instrumental|伴奏|现场)\s*$", re.I), ""),
]

_PUNCT = re.compile(r"[\s\-–—_·・,，.。!！?？'\"“”‘’:/\\|&+~`@#$%^*()\[\]{}<>；;：]+")
_FEAT = re.compile(r"\s*(?:feat|ft|with)\.?\s+.*$", re.I)


def normalize(text: str | None) -> str:
    """Aggressive normalisation: for equality checks, never for display."""
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text).lower()
    s = _FEAT.sub("", s)
    for pat, repl in _NOISE:
        s = pat.sub(repl, s)
    s = _PUNCT.sub("", s)
    return s.strip()


def primary_artist(names: list[str] | str | None) -> str:
    if not names:
        return ""
    if isinstance(names, str):
        names = [names]
    first = names[0]
    for sep in ("&", "、", "/", ",", "，"):
        if sep in first:
            first = first.split(sep)[0]
    return normalize(first)


def split_artists(raw: str | None) -> list[str]:
    """Split the multi-artist strings some platforms hand back in one field.

    ``feat``/``ft`` may or may not carry the dot; both spellings are separators,
    and the dot is part of the separator (``"A feat. B"`` is two artists, not
    ``"A"`` and ``". B"``).
    """
    if not raw:
        return []
    parts = re.split(r"\s*(?:&|、|/|,|，|；|;|\b(?:feat|ft)\b\.?)\s*", raw, flags=re.I)
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if p and p not in out:
            out.append(p)
    return out


def split_kugou_name(raw: str | None) -> tuple[list[str], str]:
    """Kugou returns "Artist - Title" (or "A & B - Title"); split it.

    Returns ``(artists, title)``. Falls back to ``([], raw)`` when there is no
    separator, so a playlist of oddly-named tracks degrades to "no artist".
    """
    if not raw:
        return [], ""
    for sep in (" - ", " – ", " — "):
        if sep in raw:
            left, right = raw.split(sep, 1)
            return split_artists(left), right.strip()
    return [], raw.strip()
