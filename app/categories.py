"""Category tree shown on Search Profiles and its default Newznab IDs.

Profiles store category *keys* (e.g. "audio.lossless"). Each indexer turns a key
into its own Newznab category IDs: by default the standard Newznab IDs below,
or a per-indexer override set on the Indexers page.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    default_ids: tuple[int, ...]

    @property
    def is_parent(self) -> bool:
        return "." not in self.key


CATEGORIES: list[Category] = [
    Category("audio", "Audio", (3000,)),
    Category("audio.mp3", "Audio > MP3", (3010,)),
    Category("audio.video", "Audio > Video", (3020,)),
    Category("audio.audiobook", "Audio > Audiobook", (3030,)),
    Category("audio.lossless", "Audio > Lossless", (3040,)),
    Category("audio.other", "Audio > Other", (3050,)),
    Category("books", "Books", (7000,)),
    Category("books.ebooks", "Books > Ebooks", (7020,)),
    Category("books.comics", "Books > Comics", (7030,)),
    Category("books.magazines", "Books > Magazines", (7010,)),
    Category("books.technical", "Books > Technical", (7040,)),
    Category("movies", "Movies", (2000,)),
    Category("movies.foreign", "Movies > Foreign", (2010,)),
    Category("movies.sd", "Movies > SD", (2030,)),
    Category("movies.hd", "Movies > HD", (2040,)),
    Category("movies.uhd", "Movies > UHD", (2045,)),
    Category("movies.bluray", "Movies > BluRay", (2050,)),
    Category("movies.3d", "Movies > 3D", (2060,)),
    Category("movies.other", "Movies > Other", (2020,)),
    Category("tv", "TV", (5000,)),
    Category("tv.sd", "TV > SD", (5030,)),
    Category("tv.hd", "TV > HD", (5040,)),
    Category("tv.uhd", "TV > UHD", (5045,)),
    Category("tv.anime", "TV > Anime", (5070,)),
    Category("tv.documentary", "TV > Documentary", (5080,)),
    Category("tv.sport", "TV > Sport", (5060,)),
    Category("pc", "PC", (4000,)),
    Category("pc.0day", "PC > 0day", (4010,)),
    Category("pc.mac", "PC > Mac", (4030,)),
    Category("pc.games", "PC > Games", (4050,)),
    Category("console", "Console", (1000,)),
    Category("other", "Other", (8000,)),
]

BY_KEY: dict[str, Category] = {c.key: c for c in CATEGORIES}


def label(key: str) -> str:
    cat = BY_KEY.get(key)
    return cat.label if cat else key


def parse_ids(text: str) -> tuple[int, ...]:
    ids: list[int] = []
    for part in str(text).replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    return tuple(ids)


def ids_for(keys: list[str], category_map: dict[str, str]) -> set[int]:
    """Resolve profile category keys into Newznab IDs for one indexer."""
    ids: set[int] = set()
    for key in keys:
        override = category_map.get(key)
        if override and parse_ids(override):
            ids.update(parse_ids(override))
        elif key in BY_KEY:
            ids.update(BY_KEY[key].default_ids)
    return ids


def category_matches(result_ids: set[int], wanted_ids: set[int]) -> bool:
    """True if any of a result's categories falls under the wanted categories.

    A standard parent category (e.g. 3000 Audio) also accepts its standard
    children (3000-3999). Custom indexer categories (>= 100000) must match
    exactly.
    """
    if not wanted_ids:
        return True  # profile has no category filter
    for rid in result_ids:
        if rid in wanted_ids:
            return True
        if rid < 100000:
            parent = (rid // 1000) * 1000
            if parent in wanted_ids:
                return True
    return False
