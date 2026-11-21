"""Keyword and phrase matching against release titles.

Profile keywords are entered one per line. No quotes are needed:

    Beatles               -> the word must appear in the title
    The Rolling Stones    -> the words must appear together and in order
                             ("The.Rolling.Stones", "the rolling stones"), or
                             run together ("TheRollingStones")
    -live                 -> exclusion: titles containing this are rejected
    -Greatest Hits        -> exclusion phrase

Quotes around a line ("The Rolling Stones") are still accepted and ignored.

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
    def queries(self) -> list[str]:
        """Search strings sent to indexers for this term.

        Multi-word terms are also searched without spaces and with dots, since
        Usenet posts are often named like "TheRollingStones" or
        "The.Rolling.Stones": "the rolling stones", "therollingstones",
        "the.rolling.stones".
        """
        if len(self.words) == 1:
            return [self.words[0]]
        return list(dict.fromkeys([" ".join(self.words), "".join(self.words), ".".join(self.words)]))

    @property
    def display(self) -> str:
        return f"-{self.raw}" if self.exclude else self.raw

    def matches_tokens(self, tokens: list[str]) -> bool:
        if not self.words:
            return False
        # "TheRollingStones" or "TheRollingStones2023" as one run-together word.
        if len(self.words) > 1:
            joined = "".join(self.words)
            if any(joined in tok for tok in tokens):
                return True
        if not self.phrase:
            present = set(tokens)
            return all(w in present for w in self.words)
        n = len(self.words)
        return any(tuple(tokens[i : i + n]) == self.words for i in range(len(tokens) - n + 1))


def parse_terms(text: str) -> list[Term]:
    """One term per line. Lines with several words are exact phrases."""
    terms: list[Term] = []
    seen: set[tuple] = set()
    for line in (text or "").splitlines():
        raw = line.strip()
        exclude = raw.startswith("-")
        raw = raw.lstrip("-").strip().strip('"').strip()
        words = tuple(tokenize(raw))
        if not words:
            continue
        key = (words, exclude)
        if key in seen:
            continue
        seen.add(key)
        terms.append(Term(raw=raw, words=words, phrase=len(words) > 1, exclude=exclude))
    return terms


def sort_keywords(text: str) -> str:
    """Tidy a profile's keyword list: one per line, A-Z (ignoring case and quotes),
    duplicates and blank lines removed, exclusions (-word) listed last."""
    lines, seen = [], set()
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        exclude = line.startswith("-")
        words = tuple(tokenize(line.lstrip("-").strip().strip('"')))
        if not words or (exclude, words) in seen:
            continue
        seen.add((exclude, words))
        lines.append((exclude, " ".join(words), line))
    lines.sort(key=lambda x: (x[0], x[1]))
    return "\n".join(x[2] for x in lines)


def match_title(title: str, terms: list[Term]) -> Term | None:
    """Return the first include-term that matches the title, or None."""
    tokens = tokenize(title)
    if any(t.exclude and t.matches_tokens(tokens) for t in terms):
        return None
    for t in terms:
        if not t.exclude and t.matches_tokens(tokens):
            return t
    return None
