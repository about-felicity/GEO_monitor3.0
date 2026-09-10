"""HTTPS client for the existing GEO remote-task server."""

from __future__ import annotations

import json
import os
import ssl
import threading
import time
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class ServerError(RuntimeError):
    pass


class ServerClient:
    def __init__(self, base_url: str, worker_token: str, timeout: int = 35):
        self.base_url = str(base_url or "").rstrip("/")
        self.worker_token = str(worker_token or "")
        self.timeout = max(5, int(timeout))
        if not self.base_url.startswith("https://"):
            raise ValueError("GEO_SERVER_URL must use HTTPS")
        if not self.worker_token:
            raise ValueError("GEO_WORKER_TOKEN is required")
        self._sessions = threading.local()
        self._connectivity_lock = threading.Lock()

    def _record_connectivity(self, connected: bool, error: str = "") -> None:
        target_value = os.environ.get("GEO_CONNECTION_HEALTH_PATH", "").strip()
        if not target_value:
            return
        target = Path(target_value).resolve()
        payload = {
            "checked_at_epoch": time.time(),
            "connected": bool(connected),
            "error": str(error)[:500],
        }
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with self._connectivity_lock:
                temporary = target.with_suffix(target.suffix + ".tmp")
                temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                temporary.replace(target)
        except OSError:
            # Health telemetry must never interrupt task collection.
            pass

    def _session(self) -> requests.Session:
        session = getattr(self._sessions, "value", None)
        if session is not None:
            return session
        session = requests.Session()
        # The enterprise worker must reach the configured HTTPS server directly.
        # Windows may expose a stale system proxy through urllib's environment
        # discovery even when no proxy process is listening.
        session.trust_env = False
        retries = Retry(
            total=4,
            connect=4,
            read=2,
            status=3,
            backoff_factor=0.6,
            status_forcelist=(408, 429, 500, 502, 503, 504),
            allowed_methods=frozenset({"POST"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retries, pool_connections=2, pool_maxsize=2)
        session.mount("https://", adapter)
        session.headers.update({
            "Authorization": f"Bearer {self.worker_token}",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "GEO-Windows-Worker-SDK/1.1",
            "Connection": "keep-alive",
        })
        self._sessions.value = session
        return session

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            ca_bundle = os.environ.get("GEO_CA_BUNDLE", "").strip()
            response = self._session().post(
                self.base_url + path,
                data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                timeout=self.timeout,
                verify=ca_bundle or True,
            )
        except requests.RequestException as exc:
            self._record_connectivity(False, f"{type(exc).__name__}: {exc}")
            raise ConnectionError(f"cannot reach GEO server: {exc}") from exc
        self._record_connectivity(True)
        if response.status_code >= 400:
            raise ServerError(
                f"server returned HTTP {response.status_code}: {response.text[:1000]}"
            )
        try:
            result = response.json()
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ServerError(f"server returned invalid JSON: {response.text[:500]}") from exc
        if not isinstance(result, dict) or result.get("ok") is False:
            raise ServerError(str(result.get("error") if isinstance(result, dict) else result))
        return result

    @staticmethod
    def _ssl_context() -> ssl.SSLContext:
        ca_bundle = os.environ.get("GEO_CA_BUNDLE", "").strip()
        if ca_bundle:
            return ssl.create_default_context(cafile=ca_bundle)
        try:
            import certifi
        except ImportError:
            return ssl.create_default_context()
        return ssl.create_default_context(cafile=certifi.where())

    def claim(self, worker_id: str, readiness: dict[str, Any]) -> dict[str, Any]:
        return self.post("/api/worker/claim", {"worker_id": worker_id, "readiness": readiness})

    def login_heartbeat(self, login_id: str, lease_token: str, message: str) -> dict[str, Any]:
        return self.post(
            f"/api/worker/logins/{login_id}/heartbeat",
            {"lease_token": lease_token, "message": str(message)[:500]},
        )

    def finish_login(
        self, login_id: str, lease_token: str, status: str, error: str = ""
    ) -> dict[str, Any]:
        return self.post(
            f"/api/worker/logins/{login_id}/finish",
            {"lease_token": lease_token, "status": status, "error": str(error)[:900]},
        )

    def heartbeat(
        self, task_id: str, lease_token: str, *, model: str = "", question: str = "", message: str
    ) -> dict[str, Any]:
        return self.post(
            f"/api/worker/tasks/{task_id}/heartbeat",
            {"lease_token": lease_token, "model": model, "question": question, "message": message},
        )

    def submit_result(
        self, task_id: str, lease_token: str, model_id: str, request_id: str, record: dict[str, Any]
    ) -> dict[str, Any]:
        return self.post(
            f"/api/worker/tasks/{task_id}/result",
            {"lease_token": lease_token, "model_id": model_id, "request_id": request_id, "record": record},
        )

    def finish(self, task_id: str, lease_token: str, status: str, error: str = "") -> dict[str, Any]:
        return self.post(
            f"/api/worker/tasks/{task_id}/finish",
            {"lease_token": lease_token, "status": status, "error": str(error)[:1800]},
        )
