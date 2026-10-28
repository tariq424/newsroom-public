"""One Chrome profile (signed in to Google once with scripts\\login-google.ps1), driven with Playwright.
A lock keeps it to one browser at a time: Chrome can't open the same profile twice."""
import threading
from contextlib import contextmanager
from pathlib import Path

LOCK = threading.Lock()
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")


class NotSignedIn(RuntimeError):
    pass


@contextmanager
def session(cfg, headless=None):
    """-> (context, page). Raises RuntimeError with a plain reason when the profile is missing or busy."""
    prof = Path(cfg["chrome_profile"])
    if not prof.exists() or not any(prof.iterdir()):
        raise NotSignedIn(f"no signed-in Google profile at {prof}: run scripts\\login-google.ps1 once")
    from playwright.sync_api import sync_playwright
    if not LOCK.acquire(timeout=300):
        raise RuntimeError("the Google browser is busy (another Newsroom task)")
    try:
        with sync_playwright() as pw:
            hl = cfg.get("headless", True) if headless is None else headless
            kw = dict(user_data_dir=str(prof), headless=hl, accept_downloads=True, viewport={"width": 1366, "height": 900},
                      args=["--disable-blink-features=AutomationControlled"])
            if hl:   # headless Chrome announces itself ("HeadlessChrome"); Google serves it a degraded / read-only Docs page
                kw["user_agent"] = UA
            elif cfg.get("offscreen_window", True):   # a real window (when headless isn't enough), parked off the screen
                kw["args"] += ["--window-position=-32000,-32000", "--window-size=1366,900"]
            if cfg.get("chrome_path"): kw["executable_path"] = cfg["chrome_path"]
            else: kw["channel"] = "chrome"
            try:
                context = pw.chromium.launch_persistent_context(**kw)
            except Exception as e:
                if "in use" in str(e).lower() or "existing browser session" in str(e).lower():
                    raise RuntimeError("the Newsroom Chrome profile is open in another window: close it and try again")
                raise
            try:
                yield context, (context.pages[0] if context.pages else context.new_page())
            finally:
                context.close()
    finally:
        LOCK.release()


def signed_out(page):
    return "accounts.google.com" in page.url or "ServiceLogin" in page.url
