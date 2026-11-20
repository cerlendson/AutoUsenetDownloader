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


# The full standard Newznab category list. Keys already used by saved
# profiles must never change.
_STANDARD: list[tuple[str, str, int]] = [
    ("console", "Console", 1000),
    ("console.nds", "Console > NDS", 1010),
    ("console.psp", "Console > PSP", 1020),
    ("console.wii", "Console > Wii", 1030),
    ("console.xbox", "Console > Xbox", 1040),
    ("console.xbox360", "Console > Xbox 360", 1050),
    ("console.wiiware", "Console > WiiWare", 1060),
    ("console.xbox360dlc", "Console > Xbox 360 DLC", 1070),
    ("console.ps3", "Console > PS3", 1080),
    ("console.other", "Console > Other", 1090),
    ("console.3ds", "Console > 3DS", 1110),
    ("console.psvita", "Console > PS Vita", 1120),
    ("console.wiiu", "Console > WiiU", 1130),
    ("console.xboxone", "Console > Xbox One", 1140),
    ("console.ps4", "Console > PS4", 1180),
    ("movies", "Movies", 2000),
    ("movies.foreign", "Movies > Foreign", 2010),
    ("movies.other", "Movies > Other", 2020),
    ("movies.sd", "Movies > SD", 2030),
    ("movies.hd", "Movies > HD", 2040),
    ("movies.uhd", "Movies > UHD", 2045),
    ("movies.bluray", "Movies > BluRay", 2050),
    ("movies.3d", "Movies > 3D", 2060),
    ("movies.dvd", "Movies > DVD", 2070),
    ("movies.webdl", "Movies > WEB-DL", 2080),
    ("audio", "Audio", 3000),
    ("audio.mp3", "Audio > MP3", 3010),
    ("audio.video", "Audio > Video", 3020),
    ("audio.audiobook", "Audio > Audiobook", 3030),
    ("audio.lossless", "Audio > Lossless", 3040),
    ("audio.other", "Audio > Other", 3050),
    ("audio.foreign", "Audio > Foreign", 3060),
    ("pc", "PC", 4000),
    ("pc.0day", "PC > 0day", 4010),
    ("pc.iso", "PC > ISO", 4020),
    ("pc.mac", "PC > Mac", 4030),
    ("pc.mobileother", "PC > Mobile-Other", 4040),
    ("pc.games", "PC > Games", 4050),
    ("pc.mobileios", "PC > Mobile-iOS", 4060),
    ("pc.mobileandroid", "PC > Mobile-Android", 4070),
    ("tv", "TV", 5000),
    ("tv.webdl", "TV > WEB-DL", 5010),
    ("tv.foreign", "TV > Foreign", 5020),
    ("tv.sd", "TV > SD", 5030),
    ("tv.hd", "TV > HD", 5040),
    ("tv.uhd", "TV > UHD", 5045),
    ("tv.other", "TV > Other", 5050),
    ("tv.sport", "TV > Sport", 5060),
    ("tv.anime", "TV > Anime", 5070),
    ("tv.documentary", "TV > Documentary", 5080),
    ("xxx", "XXX", 6000),
    ("xxx.dvd", "XXX > DVD", 6010),
    ("xxx.wmv", "XXX > WMV", 6020),
    ("xxx.xvid", "XXX > XviD", 6030),
    ("xxx.x264", "XXX > x264", 6040),
    ("xxx.uhd", "XXX > UHD", 6045),
    ("xxx.pack", "XXX > Pack", 6050),
    ("xxx.imageset", "XXX > ImageSet", 6060),
    ("xxx.other", "XXX > Other", 6070),
    ("xxx.sd", "XXX > SD", 6080),
    ("xxx.webdl", "XXX > WEB-DL", 6090),
    ("books", "Books", 7000),
    ("books.magazines", "Books > Magazines", 7010),
    ("books.ebooks", "Books > Ebooks", 7020),
    ("books.comics", "Books > Comics", 7030),
    ("books.technical", "Books > Technical", 7040),
    ("books.other", "Books > Other", 7050),
    ("books.foreign", "Books > Foreign", 7060),
    ("other", "Other", 8000),
    ("other.misc", "Other > Misc", 8010),
    ("other.hashed", "Other > Hashed", 8020),
]

CATEGORIES: list[Category] = [Category(k, lbl, (i,)) for k, lbl, i in _STANDARD]
STANDARD_IDS: set[int] = {i for _, _, i in _STANDARD}

BY_KEY: dict[str, Category] = {c.key: c for c in CATEGORIES}


# Categories an indexer reports that are not in the standard list (for example
# its own custom IDs) are stored in profiles as "ix:<indexer id>:<category id>"
# and only apply to that indexer.

def custom_key(indexer_id: int, cat_id: int | str) -> str:
    return f"ix:{indexer_id}:{cat_id}"


def parse_custom_key(key: str) -> tuple[int, int] | None:
    parts = key.split(":")
    if len(parts) == 3 and parts[0] == "ix" and parts[1].isdigit() and parts[2].isdigit():
        return int(parts[1]), int(parts[2])
    return None


def is_valid_key(key: str) -> bool:
    return key in BY_KEY or parse_custom_key(key) is not None


def custom_categories(indexer: dict) -> list[dict]:
    """Categories from an indexer's fetched caps that aren't standard Newznab IDs."""
    import json
    caps = indexer.get("caps_json") or "[]"
    caps = json.loads(caps) if isinstance(caps, str) else caps
    out = []
    for parent in caps:
        for c in [parent, *parent.get("subcats", [])]:
            cid = str(c.get("id") or "")
            if not cid.isdigit() or int(cid) in STANDARD_IDS:
                continue
            name = c.get("name") or cid
            if c is not parent:
                name = f"{parent.get('name')} > {name}"
            out.append({"key": custom_key(indexer["id"], cid), "id": int(cid), "label": name})
    return out


def label(key: str, indexer_names: dict[int, str] | None = None) -> str:
    cat = BY_KEY.get(key)
    if cat:
        return cat.label
    custom = parse_custom_key(key)
    if custom:
        ix_name = (indexer_names or {}).get(custom[0], f"indexer {custom[0]}")
        return f"{ix_name}: {custom[1]}"
    return key


def applies_to(keys: list[str], indexer_id: int) -> bool:
    """False if a profile only selected custom categories of *other* indexers."""
    if not keys:
        return True
    for key in keys:
        custom = parse_custom_key(key)
        if custom is None or custom[0] == indexer_id:
            return True
    return False


def parse_ids(text: str) -> tuple[int, ...]:
    ids: list[int] = []
    for part in str(text).replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    return tuple(ids)


def ids_for(keys: list[str], category_map: dict[str, str], indexer_id: int | None = None) -> set[int]:
    """Resolve profile category keys into Newznab IDs for one indexer."""
    ids: set[int] = set()
    for key in keys:
        custom = parse_custom_key(key)
        if custom:
            if custom[0] == indexer_id:
                ids.add(custom[1])
            continue
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
