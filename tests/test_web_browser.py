from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from monitor_core.cdp_chat import chrome_executable
from web_collectors.browser import cookie_env_name, session_cookies
from web_collectors.config import site_config


class BrowserConfigurationTests(unittest.TestCase):
    def test_explicit_chrome_path_is_supported(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            executable = Path(temporary) / "chrome"
            executable.touch()
            with patch.dict(os.environ, {"MONITOR_CHROME_PATH": str(executable)}):
                self.assertEqual(chrome_executable(), executable)

    def test_cookie_json_is_normalized_without_disk_file(self):
        site = site_config("deepseek")
        value = [{
            "name": "session", "value": "temporary", "sameSite": "no_restriction",
            "secure": True, "expirationDate": 2_000_000_000,
        }]
        with patch.dict(os.environ, {cookie_env_name(site.id): json.dumps(value)}, clear=False):
            cookies = session_cookies(site)
        self.assertEqual(cookies[0]["domain"], "chat.deepseek.com")
        self.assertEqual(cookies[0]["path"], "/")
        self.assertEqual(cookies[0]["sameSite"], "None")
        self.assertEqual(cookies[0]["expires"], 2_000_000_000)

    def test_cookie_error_never_includes_cookie_value(self):
        site = site_config("doubao")
        secret = "do-not-echo-this-cookie"
        with patch.dict(os.environ, {cookie_env_name(site.id): secret}, clear=False):
            with self.assertRaises(RuntimeError) as captured:
                session_cookies(site)
        self.assertNotIn(secret, str(captured.exception))

    def test_yuanbao_login_markers_cover_english_fresh_profile(self):
        markers = site_config("yuanbao").login_markers
        self.assertIn("Not logged in", markers)
        self.assertIn("Log In", markers)


if __name__ == "__main__":
    unittest.main()
