"""Settings that come from the environment (docker-compose.yml / .env).

Everything a user edits in the UI lives in the SQLite database under DATA_DIR,
which is mounted from the host so it survives container rebuilds and updates.
"""
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/config"))
DB_PATH = Path(os.environ.get("DB_PATH", str(DATA_DIR / "autousenet.db")))

# Optional HTTP basic auth for the web UI. Leave either blank to disable.
AUTH_USERNAME = os.environ.get("AUTH_USERNAME", "")
AUTH_PASSWORD = os.environ.get("AUTH_PASSWORD", "")

# Time zone used to display times in the UI (data is stored in UTC).
TZ = os.environ.get("TZ", "UTC")

# How often the worker wakes up to check whether anything is due (seconds).
WORKER_TICK_SECONDS = float(os.environ.get("WORKER_TICK_SECONDS", "5"))

# Timeout for outbound HTTP requests to indexers and NZBGet (seconds).
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "30"))
