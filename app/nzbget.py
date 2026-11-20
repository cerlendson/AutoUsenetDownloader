"""NZBGet JSON-RPC client (https://nzbget.com/documentation/api/)."""
from __future__ import annotations

import base64
from typing import Any

import httpx

from . import config


class NZBGetError(Exception):
    pass


# NZBGet queue states grouped into this app's statuses.
QUEUED_STATES = {"QUEUED", "PAUSED"}
DOWNLOADING_STATES = {"DOWNLOADING", "FETCHING"}


def queue_status(nzbget_status: str) -> str:
    s = (nzbget_status or "").upper()
    if s in QUEUED_STATES:
        return "queued"
    if s in DOWNLOADING_STATES:
        return "downloading"
    return "processing"  # PP_QUEUED, LOADING_PARS, VERIFYING_*, REPAIRING, UNPACKING, MOVING, ...


def history_status(nzbget_status: str) -> str:
    """Map a history Status such as 'SUCCESS/UNPACK' or 'FAILURE/PAR'."""
    s = (nzbget_status or "").upper()
    if s.startswith("SUCCESS") or s == "WARNING/SCRIPT":
        return "completed"
    return "failed"  # FAILURE/*, DELETED/*, WARNING/DAMAGED, WARNING/REPAIRABLE, ...


class NZBGetClient:
    def __init__(self, host: str, port: int | str, username: str = "", password: str = "",
                 ssl: bool = False, timeout: float | None = None,
                 transport: httpx.BaseTransport | None = None):
        host = (host or "").strip()
        for prefix in ("http://", "https://"):
            if host.lower().startswith(prefix):
                ssl = prefix == "https://"
                host = host[len(prefix):]
        host = host.rstrip("/")
        if not host:
            raise NZBGetError("NZBGet host is not set")
        scheme = "https" if ssl else "http"
        self.url = f"{scheme}://{host}:{int(port)}/jsonrpc"
        auth = httpx.BasicAuth(username, password) if (username or password) else None
        self._client = httpx.Client(timeout=timeout or config.HTTP_TIMEOUT, auth=auth,
                                    transport=transport)

    @classmethod
    def from_settings(cls, s: dict[str, Any], **kw: Any) -> "NZBGetClient":
        return cls(s.get("nzbget_host", ""), s.get("nzbget_port", 6789),
                   s.get("nzbget_username", ""), s.get("nzbget_password", ""),
                   bool(s.get("nzbget_ssl")), **kw)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "NZBGetClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def call(self, method: str, *params: Any) -> Any:
        try:
            resp = self._client.post(self.url, json={"method": method, "params": list(params), "id": 1})
        except httpx.HTTPError as exc:
            raise NZBGetError(f"Could not reach NZBGet at {self.url}: {exc}") from exc
        if resp.status_code == 401:
            raise NZBGetError("NZBGet rejected the username or password (HTTP 401)")
        if resp.status_code >= 400:
            raise NZBGetError(f"NZBGet returned HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise NZBGetError("NZBGet returned a non-JSON response") from exc
        if data.get("error"):
            err = data["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise NZBGetError(f"NZBGet error in {method}: {msg}")
        return data.get("result")

    def version(self) -> str:
        return str(self.call("version"))

    def test(self) -> str:
        version = self.version()
        status = self.call("status") or {}
        paused = " (download queue is paused)" if status.get("DownloadPaused") else ""
        return f"Connected to NZBGet {version}{paused}."

    def append(self, filename: str, content: bytes, category: str = "", priority: int = 0,
               dupe_key: str = "") -> int:
        if not filename.lower().endswith(".nzb"):
            filename += ".nzb"
        nzb_id = self.call(
            "append",
            filename,
            base64.b64encode(content).decode(),
            category or "",
            int(priority),
            False,          # AddToTop
            False,          # AddPaused
            dupe_key,       # DupeKey
            0,              # DupeScore
            "FORCE",        # DupeMode: this app does its own duplicate checks
            [],             # PPParameters
        )
        if not isinstance(nzb_id, int) or nzb_id <= 0:
            raise NZBGetError(f"NZBGet refused the NZB (append returned {nzb_id!r})")
        return nzb_id

    def list_groups(self) -> list[dict[str, Any]]:
        return self.call("listgroups", 0) or []

    def history(self) -> list[dict[str, Any]]:
        return self.call("history", False) or []
