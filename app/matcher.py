"""Keyword and phrase matching against release titles.

Profile keywords are entered one per line (commas also separate entries):

    Beatles               -> keyword: the word must appear in the title
    Pink Floyd            -> keyword: every word must appear, in any order
    "Michael Jackson"     -> exact phrase: the words must appear together, in order
    -live                 -> exclusion: titles containing this word are rejected
    -"Greatest Hits"      -> exclusion phrase

A title matches a profile if it matches ANY of its keywords/phrases and none
of its exclusions. Matching is case-insensitive and treats punctuation such as
dots, dashes and underscores (common in Usenet release names) as spaces.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_ENTRY_RE = re.compile(r'(-?)\s*"([^"]+)"|([^,\n]+)')


def tokenize(text: str) -> list[str]:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("&", " and ").replace("'", "")
    return _TOKEN_RE.findall(text)


def title_hash(title: str) -> str:
    """Stable hash of a release title that ignores case and punctuation."""
    return hashlib.sha1(" ".join(tokenize(title)).encode()).hexdigest()


@dataclass(frozen=True)
class Term:
    raw: str
    words: tuple[str, ...]
    phrase: bool
    exclude: bool = False

    @property
    def query(self) -> str:
        """Text sent to the indexer's search API."""
        return " ".join(self.words)

    @property
    def display(self) -> str:
        text = f'"{self.raw}"' if self.phrase else self.raw
        return f"-{text}" if self.exclude else text

    def matches_tokens(self, tokens: list[str]) -> bool:
        if not self.words:
            return False
        if not self.phrase:
            present = set(tokens)
            return all(w in present for w in self.words)
        n = len(self.words)
        return any(tuple(tokens[i : i + n]) == self.words for i in range(len(tokens) - n + 1))


def parse_terms(text: str) -> list[Term]:
    terms: list[Term] = []
    seen: set[tuple] = set()
    for m in _ENTRY_RE.finditer(text or ""):
        if m.group(2) is not None:
            raw, phrase, exclude = m.group(2).strip(), True, bool(m.group(1))
        else:
            raw = m.group(3).strip()
            if not raw:
                continue
            exclude = raw.startswith("-")
            raw = raw.lstrip("-").strip()
            phrase = False
            if raw.startswith('"') and raw.endswith('"') and len(raw) > 1:
                raw, phrase = raw.strip('"').strip(), True
        words = tuple(tokenize(raw))
        if not words:
            continue
        key = (words, phrase, exclude)
        if key in seen:
            continue
        seen.add(key)
        terms.append(Term(raw=raw, words=words, phrase=phrase, exclude=exclude))
    return terms


def match_title(title: str, terms: list[Term]) -> Term | None:
    """Return the first include-term that matches the title, or None."""
    tokens = tokenize(title)
    if any(t.exclude and t.matches_tokens(tokens) for t in terms):
        return None
    for t in terms:
        if not t.exclude and t.matches_tokens(tokens):
            return t
    return None
