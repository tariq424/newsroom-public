"""The Google Alerts automation against a local stand-in page whose Create Alert button stays hidden until real key
presses (the cause of the first version's HTTP 500)."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from newsroom import alerts  # noqa: E402

CHROME = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
PAGE = (Path(__file__).parent / "fixtures" / "mock_alerts.html").resolve().as_uri()


@unittest.skipUnless(Path(CHROME).is_file(), "Chromium not available")
class Alerts(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()); prof = self.tmp / "prof"; prof.mkdir(); (prof / "First Run").write_text("")
        self.cfg = {"chrome_profile": str(prof), "chrome_path": CHROME, "headless": True, "state_file": str(self.tmp / "s.json")}

    def test_creates_after_real_key_presses(self):
        with patch.object(alerts, "URL", PAGE):
            r = alerts.create(self.cfg, "Latest AI News", print)
        self.assertTrue(r["ok"]); self.assertIn("alert created", r["message"]); self.assertIn("3 alerts now", r["message"])

    def test_existing_alert_is_not_duplicated(self):
        with patch.object(alerts, "URL", PAGE):
            r = alerts.create(self.cfg, "bitcoin", print)
        self.assertIn("already exists", r["message"])

    def test_page_change_is_a_message_not_a_crash(self):
        bad = self.tmp / "bad.html"; bad.write_text("<html><body>nothing here</body></html>")
        with patch.object(alerts, "URL", bad.as_uri()):
            with self.assertRaises(alerts.AlertError) as e:
                alerts.create(self.cfg, "Latest AI News", print)
        self.assertIn("didn't behave as expected", str(e.exception))
        self.assertTrue((self.tmp / "alerts_fail.png").exists())

    def test_bad_topic(self):
        with self.assertRaises(alerts.AlertError):
            alerts.create(self.cfg, " ", print)


if __name__ == "__main__":
    unittest.main()
