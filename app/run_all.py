"""All-in-one entry point: the web UI plus the background worker in one container.

This is the image's default command, so a single `docker run` (or one container
in Unraid, Portainer, Synology...) gives you a fully working app. The worker
runs as a child process and is restarted automatically if it exits.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time

import uvicorn

log = logging.getLogger("autousenet.run_all")
_stopping = threading.Event()


def _supervise_worker() -> None:
    proc: subprocess.Popen | None = None
    while not _stopping.is_set():
        proc = subprocess.Popen([sys.executable, "-m", "app.worker"])
        while proc.poll() is None and not _stopping.is_set():
            time.sleep(1)
        if _stopping.is_set():
            break
        log.error("Worker exited with code %s; restarting in 5 seconds", proc.returncode)
        _stopping.wait(5)
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from . import db
    db.init_db()  # create/migrate the database once, before both processes start
    supervisor = threading.Thread(target=_supervise_worker, name="worker-supervisor")
    supervisor.start()
    try:
        uvicorn.run("app.web:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8080")),
                    proxy_headers=True)
    finally:
        _stopping.set()
        supervisor.join(timeout=30)


if __name__ == "__main__":
    main()
