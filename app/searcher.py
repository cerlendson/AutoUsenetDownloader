"""One scan: search every enabled profile on every enabled indexer and queue new matches."""
from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from . import categories, db, matcher
from .newznab import IndexerError, NewznabClient, Release, nzb_hash
from .nzbget import NZBGetClient, NZBGetError

log = logging.getLogger("autousenet.search")


@dataclass
class ScanStats:
    profiles: int = 0
    queries: int = 0
    results: int = 0
    matched: int = 0
    duplicates: int = 0
    queued: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        text = (f"{self.profiles} profile(s), {self.queries} search(es), {self.results} result(s), "
                f"{self.matched} match(es), {self.duplicates} already processed, {self.queued} queued")
        if self.errors:
            text += f", {len(self.errors)} error(s)"
        return text


# ------------------------------------------------------------ duplicate check

def find_duplicate(conn, indexer_id: int, rel: Release, t_hash: str,
                   n_hash: str | None = None) -> dict | None:
    """Return the existing download row if this release was already processed."""
    clauses, params = ["title_hash = ?"], [t_hash]
    if rel.guid:
        clauses.append("(indexer_id = ? AND guid = ?)")
        params += [indexer_id, rel.guid]
    if rel.nzb_id:
        clauses.append("(indexer_id = ? AND nzb_id = ?)")
        params += [indexer_id, rel.nzb_id]
    if rel.ext_hash:
        clauses.append("ext_hash = ?")
        params.append(rel.ext_hash)
    if n_hash:
        clauses.append("nzb_hash = ?")
        params.append(n_hash)
    row = conn.execute(
        f"SELECT * FROM downloads WHERE {' OR '.join(clauses)} LIMIT 1", params
    ).fetchone()
    return dict(row) if row else None


def is_processed(indexer_id: int, rel: Release, n_hash: str | None = None) -> dict | None:
    with db.connect() as conn:
        return find_duplicate(conn, indexer_id, rel, matcher.title_hash(rel.title), n_hash)


def _safe_filename(title: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", title).strip() or "download"
    return name[:200]


# ------------------------------------------------------------------- scan

NZBGetFactory = Callable[[dict[str, Any]], NZBGetClient]
IndexerFactory = Callable[[dict[str, Any]], NewznabClient]


def _default_indexer_factory(ix: dict[str, Any]) -> NewznabClient:
    return NewznabClient(ix["api_url"], ix["api_key"])


def run_scan(now: datetime | None = None,
             indexer_factory: IndexerFactory = _default_indexer_factory,
             nzbget_factory: NZBGetFactory = NZBGetClient.from_settings,
             profile_ids: list[int] | None = None,
             pause_seconds: float = 0.0,
             sleep: Callable[[float], None] = time.sleep) -> ScanStats:
    """Run one scan.

    pause_seconds: wait this long between indexer searches (used by scheduled
    scans so the indexer API isn't hammered; manual scans pass 0).
    """
    now = now or db.utcnow()
    searched_once = False
    settings = db.get_settings()
    window_hours = float(settings.get("search_window_hours") or 24)
    cutoff = now - timedelta(hours=window_hours)
    max_age_days = max(1, math.ceil(window_hours / 24))
    limit = int(settings.get("results_per_query") or 100)
    stats = ScanStats()

    with db.connect() as conn:
        indexers = db.rows_to_dicts(conn.execute(
            "SELECT * FROM indexers WHERE enabled = 1 ORDER BY priority ASC, id ASC").fetchall())
        q = "SELECT * FROM profiles WHERE enabled = 1"
        profiles = db.rows_to_dicts(conn.execute(q + " ORDER BY id").fetchall())
    if profile_ids is not None:
        profiles = [p for p in profiles if p["id"] in profile_ids]

    stats.profiles = len(profiles)
    if not profiles:
        return stats
    if not indexers:
        stats.errors.append("No enabled indexers configured")
        db.log_activity("Scan skipped: no enabled indexers are configured", "warning")
        return stats

    nzbget: NZBGetClient | None = None
    clients: dict[int, NewznabClient] = {}
    try:
        for profile in profiles:
            terms = matcher.parse_terms(profile["keywords"])
            include_terms = [t for t in terms if not t.exclude]
            if not include_terms:
                db.log_activity(f"Profile '{profile['name']}' has no keywords; skipped", "warning")
                continue
            cat_keys = json.loads(profile["categories"] or "[]")
            allowed_ix = set(json.loads(profile.get("indexer_ids") or "[]"))

            # Indexers are processed in priority order, so when the same release
            # appears on several indexers it is grabbed from the highest-priority
            # one and the others are skipped as duplicates.
            for ix in indexers:
                if allowed_ix and ix["id"] not in allowed_ix:
                    continue
                if not categories.applies_to(cat_keys, ix["id"]):
                    continue  # profile only uses other indexers' custom categories
                cat_map = json.loads(ix["category_map"] or "{}")
                wanted_ids = categories.ids_for(cat_keys, cat_map, ix["id"])
                if ix["id"] not in clients:
                    try:
                        clients[ix["id"]] = indexer_factory(ix)
                    except IndexerError as exc:
                        stats.errors.append(f"{ix['name']}: {exc}")
                        continue
                client = clients[ix["id"]]

                for term in include_terms:
                    # Search every variant of the term ("the rolling stones",
                    # "therollingstones", "the.rolling.stones") and merge the
                    # results so a post found by several variants is handled once.
                    merged: dict[str, Release] = {}
                    for query in term.queries:
                        if pause_seconds and searched_once:
                            sleep(pause_seconds)
                        searched_once = True
                        stats.queries += 1
                        try:
                            found = client.search(query, wanted_ids or None, max_age_days, limit)
                        except IndexerError as exc:
                            msg = f"{ix['name']}: search for '{query}' failed: {exc}"
                            stats.errors.append(msg)
                            db.log_activity(msg, "error")
                            continue
                        for rel in found:
                            merged.setdefault(rel.guid or rel.nzb_id or rel.link, rel)
                    results = list(merged.values())
                    stats.results += len(results)

                    for rel in results:
                        # 1. category
                        if rel.categories and not categories.category_matches(rel.categories, wanted_ids):
                            continue
                        # 2. publication date within the search window
                        if rel.pub_date is None or rel.pub_date < cutoff or rel.pub_date > now + timedelta(days=1):
                            continue
                        # 3. title vs keywords/phrases (and exclusions)
                        hit = matcher.match_title(rel.title, terms)
                        if hit is None:
                            continue
                        stats.matched += 1
                        # 4. already processed?
                        if is_processed(ix["id"], rel):
                            stats.duplicates += 1
                            continue
                        # 5. fetch NZB, final duplicate check on its contents, send to NZBGet
                        try:
                            content = client.download_nzb(rel.link)
                        except IndexerError as exc:
                            msg = f"{ix['name']}: could not fetch NZB for '{rel.title}': {exc}"
                            stats.errors.append(msg)
                            db.log_activity(msg, "error")
                            continue
                        n_hash = nzb_hash(content)
                        if is_processed(ix["id"], rel, n_hash):
                            stats.duplicates += 1
                            continue
                        if nzbget is None:
                            try:
                                nzbget = nzbget_factory(settings)
                            except NZBGetError as exc:
                                stats.errors.append(str(exc))
                                db.log_activity(f"Cannot queue downloads: {exc}", "error")
                                return stats
                        if _queue(nzbget, settings, profile, ix, rel, hit, content, n_hash, stats):
                            stats.queued += 1
            with db.connect() as conn:
                conn.execute("UPDATE profiles SET last_run_at = ? WHERE id = ?",
                             (db.iso(now), profile["id"]))
    finally:
        for c in clients.values():
            c.close()
        if nzbget is not None:
            nzbget.close()
    return stats


def _queue(nzbget: NZBGetClient, settings: dict[str, Any], profile: dict, ix: dict,
           rel: Release, hit: matcher.Term, content: bytes, n_hash: str,
           stats: ScanStats) -> bool:
    t_hash = matcher.title_hash(rel.title)
    try:
        nzbget_id = nzbget.append(
            _safe_filename(rel.title), content,
            category=settings.get("nzbget_category") or "",
            priority=int(settings.get("nzbget_priority") or 0),
            dupe_key=f"autousenet-{t_hash[:16]}",
        )
    except NZBGetError as exc:
        # Nothing was downloaded, so no row is written and the next scan retries.
        msg = f"NZBGet rejected '{rel.title}': {exc}"
        stats.errors.append(msg)
        db.log_activity(msg, "error")
        return False

    cat_text = ",".join(str(c) for c in sorted(rel.categories)) or rel.category_name
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO downloads(profile_id, profile_name, indexer_id, indexer_name, guid, nzb_id,
                   title, title_hash, nzb_hash, ext_hash, category, size, pub_date, matched_term,
                   nzbget_id, status, nzbget_status, queued_at, move_state)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (profile["id"], profile["name"], ix["id"], ix["name"], rel.guid, rel.nzb_id,
             rel.title, t_hash, n_hash, rel.ext_hash, cat_text, rel.size, db.iso(rel.pub_date),
             hit.display, nzbget_id, "queued", "QUEUED", db.iso(db.utcnow()),
             "pending" if settings.get("move_completed") else "disabled"),
        )
    db.log_activity(
        f"Queued '{rel.title}' from {ix['name']} (profile '{profile['name']}', "
        f"matched {hit.display}, NZBGet ID {nzbget_id})"
    )
    return True
