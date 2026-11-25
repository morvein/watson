"""Browser failures must remain distinct from CAPTCHA and account results."""
import asyncio
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules import amazon_web, snapchat
from modules.base import LookupResult
from modules.common import HttpClient, HttpFailure, Status, normalize


class BrowserFailureTests(unittest.TestCase):
    phone = normalize("+12025550123")

    def check(self, module, *, error=None, result=None):
        async def run():
            http = HttpClient()
            try:
                with patch.object(module, "browser_lookup", new=AsyncMock(side_effect=error, return_value=result)):
                    return await module.adapter.check(self.phone, http, {"_allow_recovery": True})
            finally:
                await http.close()
        return asyncio.run(run())

    def test_timeout_is_unknown_not_a_captcha(self):
        for module in (amazon_web, snapchat):
            with self.subTest(module=module.name if hasattr(module, "name") else module.__name__):
                result = self.check(module, error=TimeoutError("Page.goto timed out"))
                self.assertEqual(result.status, Status.UNKNOWN)
                self.assertEqual(result.error_code, f"{module.adapter.name}_timeout")
                self.assertNotIn("CAPTCHA", result.reason)
                self.assertIn("TimeoutError", result.reason)

    def test_missing_chrome_is_actionable(self):
        for module in (amazon_web, snapchat):
            result = self.check(module, error=RuntimeError("BrowserType.launch: Chromium distribution 'chrome' is not found"))
            self.assertEqual(result.status, Status.USER_ACTION_REQUIRED)
            self.assertEqual(result.error_code, f"{module.adapter.name}_chrome_not_found")
            self.assertIn("Install Chrome", result.reason)

    def test_missing_dependency_and_event_loop_have_different_causes(self):
        for module in (amazon_web, snapchat):
            missing = self.check(module, error=ModuleNotFoundError("No module named playwright"))
            loop = self.check(module, error=NotImplementedError())
            self.assertEqual(missing.error_code, "missing_browser_dependency")
            self.assertEqual(loop.error_code, f"{module.adapter.name}_unsupported_event_loop")
            self.assertEqual(missing.status, Status.USER_ACTION_REQUIRED)
            self.assertEqual(loop.status, Status.USER_ACTION_REQUIRED)

    def test_diagnostics_redact_number_url_and_profile_path(self):
        path = str(Path(tempfile.gettempdir()) / "private-browser-profile")
        for module in (amazon_web, snapchat):
            result = self.check(module, error=RuntimeError(f"Navigation {self.phone.e164} https://example.org/?token=secret {path}\nCall log: private data"))
            for private in (self.phone.e164, "token=secret", path, "Call log", "private data"):
                self.assertNotIn(private, result.reason)
            self.assertEqual(result.status, Status.UNKNOWN)

    def test_network_errors_do_not_suggest_a_captcha(self):
        for module in (amazon_web, snapchat):
            result = self.check(module, error=RuntimeError("Page.goto: net::ERR_CONNECTION_RESET at https://example.org/"))
            self.assertEqual(result.status, Status.UNKNOWN)
            self.assertEqual(result.error_code, f"{module.adapter.name}_browser_network_error")
            self.assertNotIn("CAPTCHA", result.reason)

    def test_http_limits_keep_their_existing_status(self):
        for module in (amazon_web, snapchat):
            result = self.check(module, error=HttpFailure("http_429", 429))
            self.assertEqual(result.status, Status.RATE_LIMITED)

    def test_conclusive_browser_results_are_preserved(self):
        amazon = self.check(amazon_web, result=LookupResult("not_found", "Explicit account-not-found response"))
        snap = self.check(snapchat, result=(Status.LINKED, "Password challenge"))
        self.assertEqual(amazon.status, Status.NOT_LINKED)
        self.assertEqual(snap.status, Status.LINKED)


if __name__ == "__main__":
    unittest.main()
