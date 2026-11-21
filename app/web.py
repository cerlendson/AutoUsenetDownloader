"""Web UI (FastAPI + server-rendered Jinja2 templates)."""
from __future__ import annotations

import base64
import json
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__, categories, config, db, matcher
from .newznab import IndexerError, NewznabClient, normalize_api_url
from .nzbget import NZBGetClient, NZBGetError

BASE = Path(__file__).parent


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="AutoUsenetDownloader", version=__version__, docs_url=None, redoc_url=None,
              lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")

try:
    LOCAL_TZ = ZoneInfo(config.TZ)
except Exception:  # unknown TZ string
    LOCAL_TZ = ZoneInfo("UTC")

ACTIVE_STATUSES = ("queued", "downloading", "processing")
WINDOW_CHOICES = [(6, "6 hours"), (12, "12 hours"), (24, "24 hours"), (48, "48 hours"),
                  (72, "3 days"), (168, "7 days"), (336, "14 days"), (720, "30 days")]
INTERVAL_CHOICES = [5, 10, 15, 30, 60, 120, 240, 360, 720, 1440]


# ------------------------------------------------------------------ helpers

def fmt_time(value: str | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    dt = db.parse_iso(value) if isinstance(value, str) else value
    return dt.astimezone(LOCAL_TZ).strftime(fmt) if dt else "—"


def fmt_ago(value: str | None) -> str:
    dt = db.parse_iso(value)
    if not dt:
        return "never"
    secs = int((db.utcnow() - dt).total_seconds())
    future = secs < 0
    secs = abs(secs)
    if secs < 60:
        text = f"{secs}s"
    elif secs < 3600:
        text = f"{secs // 60} min"
    elif secs < 86400:
        text = f"{secs // 3600}h {secs % 3600 // 60}m"
    else:
        text = f"{secs // 86400}d {secs % 86400 // 3600}h"
    return f"in {text}" if future else f"{text} ago"


def fmt_size(value: int | None) -> str:
    if not value:
        return "—"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return str(value)


def mask(value: str | None) -> str:
    if not value:
        return ""
    return "•" * 8 + value[-4:] if len(value) > 6 else "•" * len(value)


templates.env.filters["localtime"] = fmt_time
templates.env.filters["ago"] = fmt_ago
templates.env.filters["size"] = fmt_size
templates.env.filters["mask"] = mask
def category_label(key: str) -> str:
    """Label for a profile category key, including indexers' custom categories."""
    custom = categories.parse_custom_key(key)
    if not custom:
        return categories.label(key)
    ix = _get_indexer(custom[0])
    if not ix:
        return f"(deleted indexer) {custom[1]}"
    for c in categories.custom_categories(ix):
        if c["id"] == custom[1]:
            return f"{ix['name']}: {c['label']} ({c['id']})"
    return f"{ix['name']}: {custom[1]}"


templates.env.globals["category_label"] = category_label
templates.env.globals["version"] = __version__


def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    ctx.setdefault("msg", request.query_params.get("msg"))
    ctx.setdefault("level", request.query_params.get("level", "info"))
    ctx["request"] = request
    ctx["path"] = request.url.path
    return templates.TemplateResponse(request, name, ctx)


def redirect(url: str, msg: str | None = None, level: str = "info") -> RedirectResponse:
    if msg:
        url += ("&" if "?" in url else "?") + urlencode({"msg": msg, "level": level})
    return RedirectResponse(url, status_code=303)


def _bool(value: Any) -> bool:
    return str(value).lower() in ("1", "true", "on", "yes")


def _int(value: Any, default: int, lo: int | None = None, hi: int | None = None) -> int:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


# --------------------------------------------------------------------- auth

@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if config.AUTH_USERNAME and config.AUTH_PASSWORD and request.url.path != "/health":
        header = request.headers.get("authorization", "")
        ok = False
        if header.lower().startswith("basic "):
            try:
                user, _, pwd = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(user, config.AUTH_USERNAME) and \
                    secrets.compare_digest(pwd, config.AUTH_PASSWORD)
            except Exception:
                ok = False
        if not ok:
            return Response("Authentication required", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="AutoUsenetDownloader"'})
    return await call_next(request)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": __version__}


# ---------------------------------------------------------------- dashboard

def dashboard_stats() -> dict[str, Any]:
    s = db.get_settings()
    now_local = datetime.now(LOCAL_TZ)
    midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    with db.connect() as conn:
        one = lambda q, *p: conn.execute(q, p).fetchone()[0]  # noqa: E731
        stats = {
            "profiles_total": one("SELECT COUNT(*) FROM profiles"),
            "profiles_enabled": one("SELECT COUNT(*) FROM profiles WHERE enabled = 1"),
            "indexers_enabled": one("SELECT COUNT(*) FROM indexers WHERE enabled = 1"),
            "downloads_today": one("SELECT COUNT(*) FROM downloads WHERE queued_at >= ?",
                                   db.iso(midnight)),
            "active": one("SELECT COUNT(*) FROM downloads WHERE status IN ('queued','downloading','processing')"),
            "failed": one("SELECT COUNT(*) FROM downloads WHERE status = 'failed'"),
            "completed": one("SELECT COUNT(*) FROM downloads WHERE status = 'completed'"),
            "total": one("SELECT COUNT(*) FROM downloads"),
        }
        recent = db.rows_to_dicts(conn.execute(
            "SELECT * FROM downloads ORDER BY id DESC LIMIT 10").fetchall())
        activity = db.rows_to_dicts(conn.execute(
            "SELECT * FROM activity ORDER BY id DESC LIMIT 15").fetchall())
    hb = db.parse_iso(s.get("worker_heartbeat"))
    stats.update(
        last_scan_at=s.get("last_scan_at"),
        next_scan_at=s.get("next_scan_at"),
        last_scan_summary=s.get("last_scan_summary"),
        scan_requested=s.get("scan_requested"),
        worker_ok=bool(hb and db.utcnow() - hb < timedelta(seconds=max(60, config.WORKER_TICK_SECONDS * 6))),
        worker_heartbeat=s.get("worker_heartbeat"),
        monitor_error=s.get("monitor_error"),
        scan_interval=s.get("scan_interval_minutes"),
        search_window=s.get("search_window_hours"),
    )
    return {"stats": stats, "recent": recent, "activity": activity}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return render(request, "dashboard.html", **dashboard_stats())


@app.get("/api/status")
def api_status():
    data = dashboard_stats()
    return JSONResponse(data["stats"])


@app.post("/scan-now")
def scan_now():
    db.set_settings({"scan_requested": True})
    db.log_activity("Manual scan requested")
    return redirect("/", "Scan requested. The worker will start it within a few seconds.")


# ----------------------------------------------------------------- indexers

def _get_indexer(ix_id: int) -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM indexers WHERE id = ?", (ix_id,)).fetchone()
    return dict(row) if row else None


@app.get("/indexers", response_class=HTMLResponse)
def indexers_page(request: Request):
    with db.connect() as conn:
        rows = db.rows_to_dicts(conn.execute(
            "SELECT * FROM indexers ORDER BY priority, id").fetchall())
    return render(request, "indexers.html", indexers=rows)


def _indexer_form(request: Request, ix: dict, test_result: tuple[bool, str] | None = None):
    cat_map = ix.get("category_map")
    if isinstance(cat_map, str):
        cat_map = json.loads(cat_map or "{}")
    caps = ix.get("caps_json")
    if isinstance(caps, str):
        caps = json.loads(caps or "[]")
    return render(request, "indexer_form.html", ix=ix, cat_map=cat_map or {}, caps=caps or [],
                  categories=categories.CATEGORIES, test_result=test_result)


@app.get("/indexers/new", response_class=HTMLResponse)
def indexer_new(request: Request):
    return _indexer_form(request, {"id": None, "name": "", "api_url": "", "api_key": "",
                                   "enabled": 1, "priority": 25})


@app.get("/indexers/{ix_id}", response_class=HTMLResponse)
def indexer_edit(request: Request, ix_id: int):
    ix = _get_indexer(ix_id)
    if not ix:
        return redirect("/indexers", "Indexer not found", "error")
    return _indexer_form(request, ix)


async def _indexer_from_form(request: Request) -> dict[str, Any]:
    form = await request.form()
    ix_id = _int(form.get("id"), 0) or None
    existing = _get_indexer(ix_id) if ix_id else None
    api_key = str(form.get("api_key", "")).strip()
    if not api_key and existing:
        api_key = existing["api_key"]  # blank = keep current key
    cat_map = {}
    for cat in categories.CATEGORIES:
        value = str(form.get(f"cat_{cat.key}", "")).strip()
        if value and categories.parse_ids(value):
            cat_map[cat.key] = ",".join(str(i) for i in categories.parse_ids(value))
    return {
        "id": ix_id,
        "name": str(form.get("name", "")).strip(),
        "api_url": str(form.get("api_url", "")).strip(),
        "api_key": api_key,
        "enabled": 1 if _bool(form.get("enabled")) else 0,
        "priority": _int(form.get("priority"), 25, 1, 100),
        "category_map": cat_map,
        "caps_json": existing["caps_json"] if existing else "[]",
    }


def _test_indexer(ix: dict) -> tuple[bool, str]:
    try:
        with NewznabClient(ix["api_url"], ix["api_key"]) as client:
            return True, client.test()
    except IndexerError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"Unexpected error: {exc}"


def _record_test(ix_id: int, ok: bool, msg: str) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE indexers SET last_test_at=?, last_test_ok=?, last_test_msg=? WHERE id=?",
                     (db.iso(db.utcnow()), 1 if ok else 0, msg, ix_id))


@app.post("/indexers/save")
async def indexer_save(request: Request):
    ix = await _indexer_from_form(request)
    form = await request.form()
    if form.get("action") == "test":
        ok, msg = _test_indexer(ix)
        if ix["id"]:
            _record_test(ix["id"], ok, msg)
        return _indexer_form(request, ix, (ok, msg))
    if not ix["name"] or not ix["api_url"]:
        return _indexer_form(request, ix, (False, "Name and API URL are required."))
    try:
        normalize_api_url(ix["api_url"])
    except IndexerError as exc:
        return _indexer_form(request, ix, (False, str(exc)))
    values = (ix["name"], ix["api_url"], ix["api_key"], ix["enabled"], ix["priority"],
              json.dumps(ix["category_map"]))
    with db.connect() as conn:
        if ix["id"]:
            conn.execute("UPDATE indexers SET name=?, api_url=?, api_key=?, enabled=?, priority=?, "
                         "category_map=? WHERE id=?", (*values, ix["id"]))
        else:
            conn.execute("INSERT INTO indexers(name, api_url, api_key, enabled, priority, category_map, "
                         "created_at) VALUES (?,?,?,?,?,?,?)", (*values, db.iso(db.utcnow())))
    db.log_activity(f"Indexer '{ix['name']}' saved")
    return redirect("/indexers", f"Indexer '{ix['name']}' saved.")


@app.post("/indexers/{ix_id}/test")
def indexer_test(ix_id: int):
    ix = _get_indexer(ix_id)
    if not ix:
        return redirect("/indexers", "Indexer not found", "error")
    ok, msg = _test_indexer(ix)
    _record_test(ix_id, ok, msg)
    return redirect("/indexers", f"{ix['name']}: {msg}", "success" if ok else "error")


@app.post("/indexers/{ix_id}/caps")
def indexer_caps(ix_id: int):
    ix = _get_indexer(ix_id)
    if not ix:
        return redirect("/indexers", "Indexer not found", "error")
    try:
        with NewznabClient(ix["api_url"], ix["api_key"]) as client:
            caps = client.caps()
    except IndexerError as exc:
        return redirect(f"/indexers/{ix_id}", f"Could not fetch categories: {exc}", "error")
    with db.connect() as conn:
        conn.execute("UPDATE indexers SET caps_json=? WHERE id=?", (json.dumps(caps), ix_id))
    return redirect(f"/indexers/{ix_id}", f"Fetched {len(caps)} categories from {ix['name']}.", "success")


@app.post("/indexers/{ix_id}/toggle")
def indexer_toggle(ix_id: int):
    with db.connect() as conn:
        conn.execute("UPDATE indexers SET enabled = 1 - enabled WHERE id = ?", (ix_id,))
    return redirect("/indexers")


@app.post("/indexers/{ix_id}/delete")
def indexer_delete(ix_id: int):
    ix = _get_indexer(ix_id)
    with db.connect() as conn:
        conn.execute("DELETE FROM indexers WHERE id = ?", (ix_id,))
    if ix:
        db.log_activity(f"Indexer '{ix['name']}' deleted")
    return redirect("/indexers", "Indexer deleted.")


# ----------------------------------------------------------------- profiles

def _get_profile(pid: int) -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM profiles WHERE id = ?", (pid,)).fetchone()
    return dict(row) if row else None


@app.get("/profiles", response_class=HTMLResponse)
def profiles_page(request: Request):
    with db.connect() as conn:
        rows = db.rows_to_dicts(conn.execute("SELECT * FROM profiles ORDER BY name COLLATE NOCASE").fetchall())
        counts = {r[0]: r[1] for r in conn.execute(
            "SELECT profile_id, COUNT(*) FROM downloads GROUP BY profile_id").fetchall()}
    for p in rows:
        p["categories_list"] = json.loads(p["categories"] or "[]")
        p["terms"] = matcher.parse_terms(p["keywords"])
        p["download_count"] = counts.get(p["id"], 0)
    return render(request, "profiles.html", profiles=rows)


def _profile_form(request: Request, p: dict, test: dict | None = None):
    with db.connect() as conn:
        indexers = db.rows_to_dicts(conn.execute(
            "SELECT id, name, enabled, caps_json FROM indexers ORDER BY priority, id").fetchall())
    custom_groups = [{"indexer": ix["name"], "cats": categories.custom_categories(ix)}
                     for ix in indexers]
    custom_groups = [g for g in custom_groups if g["cats"]]
    caps_fetched = any(json.loads(ix["caps_json"] or "[]") for ix in indexers)
    p = dict(p)
    if isinstance(p.get("categories"), str):
        p["categories"] = json.loads(p["categories"] or "[]")
    if isinstance(p.get("indexer_ids"), str):
        p["indexer_ids"] = json.loads(p["indexer_ids"] or "[]")
    return render(request, "profile_form.html", p=p, categories=categories.CATEGORIES,
                  custom_groups=custom_groups, caps_fetched=caps_fetched, indexers=indexers, terms=matcher.parse_terms(p.get("keywords", "")), test=test)


@app.get("/profiles/new", response_class=HTMLResponse)
def profile_new(request: Request):
    return _profile_form(request, {"id": None, "name": "", "enabled": 1, "categories": [],
                                   "keywords": "", "indexer_ids": []})


@app.get("/profiles/{pid}", response_class=HTMLResponse)
def profile_edit(request: Request, pid: int):
    p = _get_profile(pid)
    if not p:
        return redirect("/profiles", "Profile not found", "error")
    return _profile_form(request, p)


@app.post("/profiles/save")
async def profile_save(request: Request):
    form = await request.form()
    p = {
        "id": _int(form.get("id"), 0) or None,
        "name": str(form.get("name", "")).strip(),
        "enabled": 1 if _bool(form.get("enabled")) else 0,
        "categories": [c for c in form.getlist("categories") if categories.is_valid_key(c)],
        "keywords": str(form.get("keywords", "")).replace("\r\n", "\n").strip(),
        "indexer_ids": [_int(i, 0) for i in form.getlist("indexer_ids") if _int(i, 0)],
    }
    if form.get("action") != "test":
        p["keywords"] = matcher.sort_keywords(p["keywords"])  # saved A-Z
    if form.get("action") == "test":
        title = str(form.get("test_title", "")).strip()
        hit = matcher.match_title(title, matcher.parse_terms(p["keywords"])) if title else None
        return _profile_form(request, p, {"title": title, "hit": hit})
    if not p["name"]:
        return _profile_form(request, p, {"error": "Profile name is required."})
    if not [t for t in matcher.parse_terms(p["keywords"]) if not t.exclude]:
        return _profile_form(request, p, {"error": "Add at least one keyword or phrase."})
    if not p["categories"]:
        return _profile_form(request, p, {"error": "Select at least one category."})
    values = (p["name"], p["enabled"], json.dumps(p["categories"]), p["keywords"],
              json.dumps(p["indexer_ids"]))
    with db.connect() as conn:
        if p["id"]:
            conn.execute("UPDATE profiles SET name=?, enabled=?, categories=?, keywords=?, indexer_ids=? "
                         "WHERE id=?", (*values, p["id"]))
        else:
            conn.execute("INSERT INTO profiles(name, enabled, categories, keywords, indexer_ids, created_at) "
                         "VALUES (?,?,?,?,?,?)", (*values, db.iso(db.utcnow())))
    db.log_activity(f"Search profile '{p['name']}' saved")
    return redirect("/profiles", f"Profile '{p['name']}' saved.")


@app.post("/profiles/{pid}/toggle")
def profile_toggle(pid: int):
    with db.connect() as conn:
        conn.execute("UPDATE profiles SET enabled = 1 - enabled WHERE id = ?", (pid,))
    return redirect("/profiles")


@app.post("/profiles/{pid}/delete")
def profile_delete(pid: int):
    p = _get_profile(pid)
    with db.connect() as conn:
        conn.execute("DELETE FROM profiles WHERE id = ?", (pid,))
    if p:
        db.log_activity(f"Search profile '{p['name']}' deleted")
    return redirect("/profiles", "Profile deleted. Its download history is kept for duplicate checks.")


# ------------------------------------------------------------------- nzbget

NZBGET_KEYS = ("nzbget_host", "nzbget_port", "nzbget_username", "nzbget_password", "nzbget_ssl",
               "nzbget_category", "nzbget_priority")


async def _nzbget_from_form(request: Request) -> dict[str, Any]:
    form = await request.form()
    current = db.get_settings()
    password = str(form.get("nzbget_password", ""))
    if not password and not _bool(form.get("clear_password")):
        password = current.get("nzbget_password", "")
    return {
        "nzbget_host": str(form.get("nzbget_host", "")).strip(),
        "nzbget_port": _int(form.get("nzbget_port"), 6789, 1, 65535),
        "nzbget_username": str(form.get("nzbget_username", "")).strip(),
        "nzbget_password": password,
        "nzbget_ssl": _bool(form.get("nzbget_ssl")),
        "nzbget_category": str(form.get("nzbget_category", "")).strip(),
        "nzbget_priority": _int(form.get("nzbget_priority"), 0, -100, 900),
    }


@app.get("/nzbget", response_class=HTMLResponse)
def nzbget_page(request: Request):
    return render(request, "nzbget.html", s=db.get_settings(), test_result=None)


@app.post("/nzbget")
async def nzbget_save(request: Request):
    values = await _nzbget_from_form(request)
    form = await request.form()
    if form.get("action") == "test":
        try:
            with NZBGetClient.from_settings(values) as client:
                result = (True, client.test())
        except NZBGetError as exc:
            result = (False, str(exc))
        s = {**db.get_settings(), **values}
        return render(request, "nzbget.html", s=s, test_result=result)
    db.set_settings(values)
    db.log_activity("NZBGet settings saved")
    return redirect("/nzbget", "NZBGet settings saved.", "success")


# ---------------------------------------------------------------- downloads

@app.get("/downloads", response_class=HTMLResponse)
def downloads_page(request: Request, status: str = "", q: str = "", page: int = 1):
    page = max(1, page)
    per_page = 50
    where, params = [], []
    if status == "active":
        where.append("status IN ('queued','downloading','processing')")
    elif status:
        where.append("status = ?")
        params.append(status)
    if q:
        where.append("(title LIKE ? OR profile_name LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with db.connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM downloads {clause}", params).fetchone()[0]
        rows = db.rows_to_dicts(conn.execute(
            f"SELECT * FROM downloads {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, per_page, (page - 1) * per_page)).fetchall())
    pages = max(1, (total + per_page - 1) // per_page)
    return render(request, "downloads.html", downloads=rows, status=status, q=q, page=page,
                  pages=pages, total=total)


@app.post("/downloads/{dl_id}/forget")
def download_forget(dl_id: int):
    with db.connect() as conn:
        row = conn.execute("SELECT title FROM downloads WHERE id = ?", (dl_id,)).fetchone()
        conn.execute("DELETE FROM downloads WHERE id = ?", (dl_id,))
    if row:
        db.log_activity(f"Removed '{row['title']}' from history; it may be downloaded again")
    return redirect("/downloads", "Removed from history. If it still matches, the next scan will grab it again.")


@app.post("/downloads/{dl_id}/retry-move")
def download_retry_move(dl_id: int):
    with db.connect() as conn:
        conn.execute("UPDATE downloads SET move_state='pending', message=NULL WHERE id = ? AND status='completed'",
                     (dl_id,))
    return redirect("/downloads", "The move will be retried on the next NZBGet check.")


# ----------------------------------------------------------------- settings

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    return render(request, "settings.html", s=db.get_settings(), window_choices=WINDOW_CHOICES,
                  interval_choices=INTERVAL_CHOICES)


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    current = db.get_settings()
    interval = _int(form.get("scan_interval_minutes"), 30, 1, 10080)
    values = {
        "scan_interval_minutes": interval,
        "search_window_hours": _int(form.get("search_window_hours"), 24, 1, 24 * 365),
        "monitor_interval_seconds": _int(form.get("monitor_interval_seconds"), 60, 10, 3600),
        "results_per_query": _int(form.get("results_per_query"), 100, 10, 500),
        "move_completed": _bool(form.get("move_completed")),
        "target_dir": str(form.get("target_dir", "")).strip() or "/target",
        "target_subfolder_per_profile": _bool(form.get("target_subfolder_per_profile")),
        "nzbget_path_prefix": str(form.get("nzbget_path_prefix", "")).strip(),
        "local_path_prefix": str(form.get("local_path_prefix", "")).strip(),
    }
    # Re-plan the next scan when the interval changes.
    last = db.parse_iso(current.get("last_scan_at"))
    if last and interval != current.get("scan_interval_minutes"):
        values["next_scan_at"] = db.iso(last + timedelta(minutes=interval))
    db.set_settings(values)
    if values["move_completed"] and not current.get("move_completed"):
        # Downloads that completed while moving was off are not moved retroactively.
        with db.connect() as conn:
            conn.execute("UPDATE downloads SET move_state='pending' WHERE move_state='disabled' "
                         "AND status IN ('queued','downloading','processing')")
    db.log_activity("Settings saved")
    return redirect("/settings", "Settings saved.", "success")


# ----------------------------------------------------------------- activity

@app.get("/activity", response_class=HTMLResponse)
def activity_page(request: Request, level: str = ""):
    with db.connect() as conn:
        if level:
            rows = conn.execute("SELECT * FROM activity WHERE level = ? ORDER BY id DESC LIMIT 500",
                                (level,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM activity ORDER BY id DESC LIMIT 500").fetchall()
    return render(request, "activity.html", rows=db.rows_to_dicts(rows), level=level)
