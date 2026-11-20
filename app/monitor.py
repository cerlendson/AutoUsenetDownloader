"""Track this app's downloads in NZBGet and move completed ones to the target folder.

Only rows in our own `downloads` table are looked up, by their NZBGet ID, so
downloads added to NZBGet by anything else are never touched.
"""
from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Any

from . import db
from .nzbget import NZBGetClient, NZBGetError, history_status, queue_status

log = logging.getLogger("autousenet.monitor")

ACTIVE = ("queued", "downloading", "processing")


def map_path(path: str, settings: dict[str, Any]) -> str:
    """Translate a path as NZBGet sees it into the path mounted in this container."""
    remote = (settings.get("nzbget_path_prefix") or "").rstrip("/\\")
    local = (settings.get("local_path_prefix") or "").rstrip("/\\")
    if remote and (path == remote or path.startswith(remote + "/") or path.startswith(remote + "\\")):
        rest = path[len(remote):].replace("\\", "/")
        return local + rest
    return path


def _sanitize(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", name).strip() or "unnamed"


def _unique(dest: Path) -> Path:
    if not dest.exists():
        return dest
    for i in range(2, 1000):
        candidate = dest.with_name(f"{dest.name} ({i})")
        if not candidate.exists():
            return candidate
    raise OSError(f"Too many existing copies of {dest}")


def move_completed(row: dict[str, Any], settings: dict[str, Any]) -> tuple[str, str | None, str]:
    """Move a completed download. Returns (move_state, moved_to, message)."""
    src_text = row.get("source_dir") or ""
    if not src_text:
        return "error", None, "NZBGet did not report a destination folder"
    src = Path(map_path(src_text, settings))
    if not src.exists():
        return "error", None, (f"Completed folder not found at {src}. Check that it is mounted "
                               f"into the container and that the path mapping is right.")
    target = Path(settings.get("target_dir") or "/target")
    if settings.get("target_subfolder_per_profile") and row.get("profile_name"):
        target = target / _sanitize(row["profile_name"])
    target.mkdir(parents=True, exist_ok=True)
    dest = _unique(target / src.name)
    shutil.move(str(src), str(dest))
    return "done", str(dest), f"Moved to {dest}"


def run_monitor(nzbget: NZBGetClient | None = None) -> dict[str, int]:
    settings = db.get_settings()
    counts = {"updated": 0, "completed": 0, "failed": 0, "moved": 0}

    with db.connect() as conn:
        tracked = db.rows_to_dicts(conn.execute(
            "SELECT * FROM downloads WHERE nzbget_id IS NOT NULL AND "
            "(status IN ('queued','downloading','processing') OR "
            " (status = 'completed' AND move_state IN ('pending','error')))"
        ).fetchall())
    if not tracked:
        return counts

    own_client = nzbget is None
    try:
        if nzbget is None:
            nzbget = NZBGetClient.from_settings(settings)
        active = [r for r in tracked if r["status"] in ACTIVE]
        if active:
            queue = {g["NZBID"]: g for g in nzbget.list_groups()}
            history = {h["NZBID"]: h for h in nzbget.history() if h.get("Kind", "NZB") == "NZB"}
            for row in active:
                _update_row(row, queue.get(row["nzbget_id"]), history.get(row["nzbget_id"]), counts)
    except NZBGetError as exc:
        log.warning("NZBGet monitor failed: %s", exc)
        db.set_settings({"monitor_error": str(exc)})
        return counts
    finally:
        if own_client and nzbget is not None:
            nzbget.close()
    db.set_settings({"monitor_error": "", "last_monitor_at": db.iso(db.utcnow())})

    if settings.get("move_completed"):
        with db.connect() as conn:
            to_move = db.rows_to_dicts(conn.execute(
                "SELECT * FROM downloads WHERE status = 'completed' AND move_state IN ('pending','error')"
            ).fetchall())
        for row in to_move:
            try:
                state, moved_to, msg = move_completed(row, settings)
            except PermissionError as exc:
                state, moved_to, msg = "error", None, (
                    f"Move failed: {exc}. Make sure PUID/PGID in .env can write to the target "
                    f"folder and to NZBGet's completed folder.")
            except OSError as exc:
                state, moved_to, msg = "error", None, f"Move failed: {exc}"
            if state == "error" and row.get("move_state") == "error" and row.get("message") == msg:
                continue  # same error as last time, don't spam the activity log
            with db.connect() as conn:
                conn.execute("UPDATE downloads SET move_state=?, moved_to=?, message=? WHERE id=?",
                             (state, moved_to, msg, row["id"]))
            if state == "done":
                counts["moved"] += 1
                db.log_activity(f"Moved '{row['title']}' to {moved_to}")
            else:
                db.log_activity(f"Could not move '{row['title']}': {msg}", "error")
    return counts


def _update_row(row: dict[str, Any], group: dict | None, hist: dict | None,
                counts: dict[str, int]) -> None:
    fields: dict[str, Any] = {}
    if group is not None:
        nz_status = group.get("Status", "")
        fields = {"status": queue_status(nz_status), "nzbget_status": nz_status}
    elif hist is not None:
        nz_status = hist.get("Status", "")
        new_status = history_status(nz_status)
        fields = {
            "status": new_status,
            "nzbget_status": nz_status,
            "completed_at": db.iso(db.utcnow()),
            "source_dir": hist.get("FinalDir") or hist.get("DestDir") or "",
        }
        if new_status == "failed":
            fields["move_state"] = "disabled" if row.get("move_state") == "disabled" else "skipped"
            fields["message"] = f"NZBGet reported {nz_status}"
    else:
        # Not in queue or history: removed from NZBGet by someone. Treat as failed.
        fields = {"status": "failed", "nzbget_status": "MISSING",
                  "completed_at": db.iso(db.utcnow()),
                  "message": "No longer in NZBGet queue or history (deleted outside this app?)"}

    changed = {k: v for k, v in fields.items() if row.get(k) != v}
    if not changed:
        return
    with db.connect() as conn:
        sets = ", ".join(f"{k} = ?" for k in changed)
        conn.execute(f"UPDATE downloads SET {sets} WHERE id = ?", (*changed.values(), row["id"]))
    counts["updated"] += 1
    if changed.get("status") == "completed":
        counts["completed"] += 1
        db.log_activity(f"Download completed: '{row['title']}' ({fields['nzbget_status']})")
    elif changed.get("status") == "failed":
        counts["failed"] += 1
        db.log_activity(f"Download failed: '{row['title']}' ({fields['nzbget_status']})", "error")
