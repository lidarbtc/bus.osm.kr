"""Client for the dashboard Worker's runner API."""

from __future__ import annotations

import gzip
import os
import shutil
import tempfile
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class ControlError(RuntimeError):
    pass


class ControlClient:
    def __init__(self, base_url: str, token: str, version: str = "", session=None):
        parts = urlsplit(base_url)
        if parts.scheme != "https" or not parts.hostname or not token:
            raise ControlError("BUS_SYNC_CONTROL_URL must be an https URL and BUS_SYNC_RUNNER_TOKEN must be set")
        self.base = base_url.rstrip("/")
        self.token = token
        self.version = version[:64]
        if session is None:
            session = requests.Session()
            retry = Retry(total=3, backoff_factor=2, status_forcelist=[502, 503, 504],
                          allowed_methods=frozenset({"GET", "PUT"}))
            session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session = session

    @classmethod
    def from_env(cls) -> "ControlClient":
        return cls(os.environ.get("BUS_SYNC_CONTROL_URL", ""), os.environ.get("BUS_SYNC_RUNNER_TOKEN", ""),
                   os.environ.get("BUS_SYNC_VERSION", ""))

    def _request(self, method: str, path: str, **kwargs) -> dict:
        headers = {"Authorization": f"Bearer {self.token}", "X-Runner-Version": self.version,
                   "User-Agent": "osm-korea-bus-sync-runner", **kwargs.pop("headers", {})}
        try:
            response = self.session.request(method, self.base + path, headers=headers, timeout=(15, 300), **kwargs)
        except requests.RequestException as exc:
            raise ControlError(f"{method} {path} failed: {exc}") from exc
        if response.status_code >= 400:
            raise ControlError(f"{method} {path} failed: {response.status_code} {response.text[:300]}")
        return response.json()

    def control(self) -> dict:
        data = self._request("GET", "/api/runner/control")
        if not isinstance(data.get("stopped"), bool) or not isinstance(data.get("rollback_jobs"), list):
            raise ControlError("control response has an unexpected shape")
        return data

    def update_run(self, run_id: str, **fields) -> None:
        self._request("POST", f"/api/runner/runs/{quote(run_id)}", json={k: v for k, v in fields.items() if v is not None})

    def update_job(self, job_id: int, status: str, result=None, revert_changeset=None) -> None:
        body = {"status": status, "result": result, "revert_changeset": revert_changeset}
        self._request("POST", f"/api/runner/jobs/{int(job_id)}", json={k: v for k, v in body.items() if v is not None})

    def register_changeset(self, changeset_id: int, run_id: str, comment: str) -> None:
        self._request("POST", "/api/runner/changesets", json={"id": changeset_id, "run_id": run_id, "comment": comment})

    def upload(self, run_id: str, path: Path) -> None:
        with tempfile.TemporaryFile() as compressed:
            with Path(path).open("rb") as source, gzip.GzipFile(fileobj=compressed, mode="wb", mtime=0) as target:
                shutil.copyfileobj(source, target, 1024 * 1024)
            size = compressed.tell()
            compressed.seek(0)
            self._request("PUT", f"/api/runner/runs/{quote(run_id)}/files/{quote(Path(path).name)}", data=compressed,
                          headers={"Content-Type": "application/octet-stream", "Content-Length": str(size)})
