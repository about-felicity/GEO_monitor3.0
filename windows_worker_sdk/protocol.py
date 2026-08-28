"""HTTPS client for the existing GEO remote-task server."""

from __future__ import annotations

import json
import ssl
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


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

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            self.base_url + path,
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.worker_token}",
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "GEO-Windows-Worker-SDK/1.0",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout, context=ssl.create_default_context()) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:1000]
            raise ServerError(f"server returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise ConnectionError(f"cannot reach GEO server: {exc.reason}") from exc
        if not isinstance(result, dict) or result.get("ok") is False:
            raise ServerError(str(result.get("error") if isinstance(result, dict) else result))
        return result

    def claim(self, worker_id: str, readiness: dict[str, Any]) -> dict[str, Any]:
        return self.post("/api/worker/claim", {"worker_id": worker_id, "readiness": readiness})

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

