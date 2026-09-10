from __future__ import annotations

import socket
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from doubao_dashboard_server import DashboardHandler, HighConcurrencyHTTPServer


ROOT = Path(__file__).resolve().parents[1]


class ServerLifecycleTests(unittest.TestCase):
    @staticmethod
    def authorization_handler(identity, original_uri):
        handler = object.__new__(DashboardHandler)
        handler.path = "/api/admin/authorize"
        handler.headers = {"X-Original-URI": original_uri}
        handler.admin_identity = MagicMock(return_value=identity)
        handler.send_bytes = MagicMock()
        handler.do_GET()
        return handler

    def test_manager_gets_forbidden_instead_of_login_redirect_for_tasks_page(self):
        handler = self.authorization_handler(
            {"id": "manager-1", "role": "manager"}, "/geo/tasks"
        )
        handler.send_bytes.assert_called_once_with(
            b"", "text/plain; charset=utf-8", 403
        )

    def test_anonymous_user_still_gets_login_challenge_for_tasks_page(self):
        handler = self.authorization_handler(None, "/geo/tasks")
        handler.send_bytes.assert_called_once_with(
            b"", "text/plain; charset=utf-8", 401
        )

    def test_hybrid_startup_guards_frontend_assets_and_script_permissions(self):
        compose = (ROOT / "deploy/hybrid/docker-compose.yml").read_text(encoding="utf-8")
        entrypoint = (ROOT / "deploy/hybrid/entrypoint.sh").read_text(encoding="utf-8")
        self.assertIn('["/usr/bin/tini", "--", "/bin/sh", "/app/entrypoint.sh"]', compose)
        self.assertIn("verify-production-assets.mjs", compose)
        self.assertIn("verify-production-assets.mjs", entrypoint)

    def test_occupied_port_preserves_original_bind_error(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        try:
            with self.assertRaises(OSError):
                HighConcurrencyHTTPServer(("127.0.0.1", port), DashboardHandler)
        finally:
            blocker.close()


if __name__ == "__main__":
    unittest.main()
