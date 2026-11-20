"""Background worker: runs scheduled scans and monitors NZBGet.

Run with `python -m app.worker`. In Docker Compose this is the `worker` service.
"""
from __future__ import annotations

import logging
import signal
import time
from datetime import timedelta

from . import config, db
from .monitor import run_monitor
from .searcher import run_scan

log = logging.getLogger("autousenet.worker")
_stop = False


def _handle_stop(signum, frame):  # noqa: ARG001
    global _stop
    _stop = True


def scan_once() -> None:
    started = db.utcnow()
    log.info("Starting scan")
    try:
        stats = run_scan(now=started)
        summary = stats.summary()
        level = "warning" if stats.errors else "info"
    except Exception as exc:  # keep the worker alive whatever happens
        log.exception("Scan crashed")
        summary, level = f"Scan failed: {exc}", "error"
    interval = float(db.get_setting("scan_interval_minutes") or 30)
    finished = db.utcnow()
    db.set_settings({
        "last_scan_at": db.iso(started),
        "last_scan_summary": summary,
        "next_scan_at": db.iso(finished + timedelta(minutes=interval)),
        "scan_requested": False,
    })
    db.log_activity(f"Scan finished: {summary}", level)
    log.info("Scan finished: %s", summary)


def tick(last_monitor: float) -> float:
    settings = db.get_settings()
    now = db.utcnow()
    db.set_settings({"worker_heartbeat": db.iso(now)})

    next_scan = db.parse_iso(settings.get("next_scan_at"))
    if settings.get("scan_requested") or next_scan is None or now >= next_scan:
        scan_once()

    monitor_every = float(settings.get("monitor_interval_seconds") or 60)
    if time.monotonic() - last_monitor >= monitor_every:
        try:
            counts = run_monitor()
            if any(counts.values()):
                log.info("Monitor: %s", counts)
        except Exception:
            log.exception("Monitor crashed")
        last_monitor = time.monotonic()
    return last_monitor


def _acquire_lock():
    """Allow only one worker per database, even across containers.

    Returns the open lock file, or None if asked to stop while waiting.
    Another worker (e.g. an old separate `worker` container next to the
    all-in-one container) just waits on standby.
    """
    import fcntl
    path = config.DB_PATH.parent / "worker.lock"
    f = open(path, "a+")
    announced = False
    while not _stop:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except OSError:
            if not announced:
                log.info("Another worker is already running for this database; standing by")
                announced = True
            time.sleep(5)
    f.close()
    return None


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs full request URLs at INFO, which would put indexer API keys in the logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    db.init_db()
    lock = _acquire_lock()
    if lock is None:
        log.info("Worker stopped")
        return
    db.log_activity("Worker started")
    log.info("Worker started; database at %s", config.DB_PATH)
    last_monitor = 0.0
    while not _stop:
        try:
            last_monitor = tick(last_monitor)
        except Exception:
            log.exception("Worker tick failed")
        for _ in range(int(config.WORKER_TICK_SECONDS * 10)):
            if _stop:
                break
            time.sleep(0.1)
    log.info("Worker stopped")


if __name__ == "__main__":
    main()
