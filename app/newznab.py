"""Minimal Newznab API client (NZBGeek, NZBFinder, DrunkenSlug, NZBPlanet, ...)."""
from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qs, urlparse, urlsplit, urlunsplit

import httpx

from . import config

USER_AGENT = "AutoUsenetDownloader/1.0"


class IndexerError(Exception):
    pass


@dataclass
class Release:
    title: str
    guid: str
    nzb_id: str
    link: str
    pub_date: datetime | None
    categories: set[int] = field(default_factory=set)
    size: int | None = None
    ext_hash: str | None = None
    category_name: str = ""


def normalize_api_url(url: str) -> str:
    """Accept 'https://nzbgeek.info', 'https://api.nzbgeek.info/api' or '.../api/'."""
    url = url.strip()
    if not url:
        raise IndexerError("API URL is empty")
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    if not path.endswith("/api"):
        path = path + "/api"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _derive_nzb_id(guid: str, link: str) -> str:
    for url in (link, guid):
        if not url:
            continue
        try:
            q = parse_qs(urlparse(url).query)
        except ValueError:
            continue
        for key in ("id", "guid"):
            if q.get(key):
                return q[key][0]
    if guid:
        return guid.rstrip("/").rsplit("/", 1)[-1]
    return ""


def _parse_date(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text.strip())
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(text.strip())
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _check_error(root: ET.Element) -> None:
    if _local(root.tag) == "error":
        code = root.get("code", "?")
        desc = root.get("description", "unknown error")
        raise IndexerError(f"Indexer error {code}: {desc}")


def _parse_xml(text: str) -> ET.Element:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        snippet = text.strip()[:200].replace("\n", " ")
        raise IndexerError(f"Indexer returned invalid XML ({exc}): {snippet}") from exc
    _check_error(root)
    return root


def parse_results(xml_text: str) -> list[Release]:
    root = _parse_xml(xml_text)
    releases: list[Release] = []
    for item in root.iter():
        if _local(item.tag) != "item":
            continue
        data: dict[str, Any] = {"categories": set()}
        attrs: dict[str, list[str]] = {}
        enclosure_url = None
        for child in item:
            name = _local(child.tag)
            if name == "title":
                data["title"] = (child.text or "").strip()
            elif name == "guid":
                data["guid"] = (child.text or "").strip()
            elif name == "link":
                data["link"] = (child.text or "").strip()
            elif name == "pubDate":
                data["pub_date"] = _parse_date(child.text)
            elif name == "category":
                data["category_name"] = (child.text or "").strip()
            elif name == "enclosure":
                enclosure_url = child.get("url")
                if child.get("length", "").isdigit():
                    data.setdefault("size", int(child.get("length")))
            elif name == "attr":
                attrs.setdefault(child.get("name", "").lower(), []).append(child.get("value", ""))
        for value in attrs.get("category", []):
            if value.strip().isdigit():
                data["categories"].add(int(value))
        if attrs.get("size") and attrs["size"][0].isdigit():
            data["size"] = int(attrs["size"][0])
        if not data.get("pub_date") and attrs.get("usenetdate"):
            data["pub_date"] = _parse_date(attrs["usenetdate"][0])
        for key in ("nzbhash", "hash", "infohash"):
            if attrs.get(key) and attrs[key][0]:
                data["ext_hash"] = attrs[key][0].lower()
                break
        link = data.get("link") or enclosure_url or ""
        if enclosure_url and enclosure_url.startswith("http"):
            link = enclosure_url
        guid = data.get("guid") or link
        title = data.get("title", "")
        if not title or not link:
            continue
        nzb_id = (attrs.get("guid") or [""])[0] or _derive_nzb_id(guid, link)
        releases.append(
            Release(
                title=title,
                guid=guid,
                nzb_id=nzb_id,
                link=link,
                pub_date=data.get("pub_date"),
                categories=data["categories"],
                size=data.get("size"),
                ext_hash=data.get("ext_hash"),
                category_name=data.get("category_name", ""),
            )
        )
    return releases


def parse_caps(xml_text: str) -> list[dict[str, Any]]:
    root = _parse_xml(xml_text)
    cats: list[dict[str, Any]] = []
    for el in root.iter():
        if _local(el.tag) != "category":
            continue
        parent = {"id": el.get("id"), "name": el.get("name"), "subcats": []}
        for sub in el:
            if _local(sub.tag) == "subcat":
                parent["subcats"].append({"id": sub.get("id"), "name": sub.get("name")})
        cats.append(parent)
    return cats


class NewznabClient:
    def __init__(self, api_url: str, api_key: str, timeout: float | None = None,
                 transport: httpx.BaseTransport | None = None):
        self.api_url = normalize_api_url(api_url)
        self.api_key = api_key.strip()
        self._client = httpx.Client(
            timeout=timeout or config.HTTP_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "NewznabClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _get(self, params: dict[str, Any]) -> str:
        params = {k: v for k, v in params.items() if v not in (None, "")}
        if self.api_key:
            params["apikey"] = self.api_key
        try:
            resp = self._client.get(self.api_url, params=params)
        except httpx.HTTPError as exc:
            raise IndexerError(f"Could not reach indexer: {exc}") from exc
        if resp.status_code in (401, 403):
            raise IndexerError(f"Indexer rejected the API key (HTTP {resp.status_code})")
        if resp.status_code >= 400:
            # Some indexers send a Newznab <error> body with a 4xx/5xx status.
            try:
                _parse_xml(resp.text)
            except IndexerError as exc:
                raise IndexerError(f"HTTP {resp.status_code}: {exc}") from exc
            raise IndexerError(f"Indexer returned HTTP {resp.status_code}")
        return resp.text

    def caps(self) -> list[dict[str, Any]]:
        return parse_caps(self._get({"t": "caps"}))

    def search(self, query: str, categories: set[int] | None = None,
               max_age_days: int | None = None, limit: int = 100) -> list[Release]:
        params = {
            "t": "search",
            "q": query,
            "cat": ",".join(str(c) for c in sorted(categories)) if categories else None,
            "maxage": max_age_days,
            "limit": limit,
            "extended": 1,
        }
        return parse_results(self._get(params))

    def test(self) -> str:
        """Check the URL and API key. Returns a human-readable success message."""
        cats = self.caps()
        # caps often works without a key, so also run a tiny search to check the key.
        results = self.search("", limit=1)
        return f"Connected. {len(cats)} categories available; search returned {len(results)} result(s)."

    def download_nzb(self, link: str) -> bytes:
        try:
            resp = self._client.get(link)
        except httpx.HTTPError as exc:
            raise IndexerError(f"Could not download NZB: {exc}") from exc
        if resp.status_code >= 400:
            raise IndexerError(f"NZB download failed with HTTP {resp.status_code}")
        content = resp.content
        if b"<nzb" not in content[:4096].lower():
            try:
                _parse_xml(resp.text)
            except IndexerError as exc:
                raise IndexerError(f"NZB download failed: {exc}") from exc
            raise IndexerError("Indexer did not return an NZB file (grab limit reached?)")
        return content


def nzb_hash(content: bytes) -> str:
    """Hash of an NZB's segment list, so the same post from two indexers matches."""
    try:
        root = ET.fromstring(content)
        segs = sorted(
            (el.text or "").strip() for el in root.iter() if _local(el.tag) == "segment"
        )
        if segs:
            return hashlib.sha256("\n".join(segs).encode()).hexdigest()
    except ET.ParseError:
        pass
    return hashlib.sha256(content).hexdigest()


