"""Create a Google Alert for a topic in the signed-in Chrome profile (google.com/alerts).
The alert is delivered to the account's Gmail, where Muse picks it up. Never types a password: the profile must already
be signed in (scripts\\login-google.ps1).

Page facts (October 2026): the topic box is `#query_div input`; the "Create Alert" button (`#create_alert`) sits in a
hidden `.alert_buttons` panel that Google only shows after real key presses in the box (fill() sets the text but fires no
key events, which is why the first version timed out on an invisible button). "My alerts (N)" counts the alerts."""
import re
import time
from pathlib import Path

from . import browser

URL = "https://www.google.com/alerts"


class AlertError(RuntimeError):
    pass


def _count(page):
    try:
        m = re.search(r"My alerts\s*\((\d+)\)", page.locator("body").inner_text(timeout=5000))
        return int(m.group(1)) if m else None
    except Exception:
        return None


def _existing(page):
    """Topics of the alerts already set up (text of each alert row)."""
    try:
        return page.evaluate("""() => Array.from(document.querySelectorAll('#manage-alerts-div li.alert_instance .query_div > span'))
            .map(e => e.innerText.trim()).filter(Boolean)""")
    except Exception:
        return []


def create(cfg, topic, log=print):
    """-> {"ok", "message"}. Raises AlertError with a plain reason (never anything else)."""
    topic = re.sub(r"\s+", " ", str(topic or "")).strip()
    if not 2 <= len(topic) <= 200:
        raise AlertError("type a topic (2-200 characters)")
    evidence = Path(cfg.get("state_file", "newsroom_state.json")).parent / "alerts_fail"
    try:
        with browser.session(cfg, headless=cfg.get("alerts_headless", cfg.get("headless", True))) as (context, page):
            try:
                return _create(page, topic, log)
            except AlertError:
                _evidence(page, evidence); raise
            except Exception as e:
                _evidence(page, evidence)
                raise AlertError(f"Google Alerts page didn't behave as expected ({str(e).splitlines()[0][:160]}); "
                                 f"screenshot {evidence}.png")
    except AlertError:
        raise
    except browser.NotSignedIn as e:
        raise AlertError(str(e))
    except Exception as e:   # browser couldn't start, profile busy...
        raise AlertError(str(e).splitlines()[0][:200])


def _create(page, topic, log):
    page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2500)
    if browser.signed_out(page) or "accounts.google.com" in page.url:
        raise AlertError("the Newsroom Chrome profile is not signed in to Google: run scripts\\login-google.ps1")
    box = page.locator("#query_div input, input[aria-label*='Create an alert' i]").first
    box.wait_for(state="visible", timeout=20000)
    if topic.lower() in [t.lower().strip('"') for t in _existing(page)]:
        return {"ok": True, "message": f"an alert for \"{topic}\" already exists"}
    before = _count(page)
    box.click()
    box.fill("")
    box.press_sequentially(topic, delay=60)   # real key presses: Google opens the options panel on keyup
    box.press("End")
    btn = page.locator("#create_alert")
    try:
        btn.wait_for(state="visible", timeout=20000)
    except Exception:
        raise AlertError("Google didn't show the Create Alert button after typing the topic (the page may have changed)")
    page.wait_for_timeout(1500)   # the results preview loads under the box
    btn.click()
    t0 = time.time()
    while time.time() - t0 < 25:
        page.wait_for_timeout(1500)
        after = _count(page)
        if before is not None and after is not None and after > before:
            log(f"Google Alert created: {topic} (my alerts: {before} -> {after})")
            return {"ok": True, "message": f"alert created for \"{topic}\" (delivered to your Gmail; {after} alerts now)"}
        if topic.lower() in [t.lower().strip('"') for t in _existing(page)]:
            log(f"Google Alert created: {topic}")
            return {"ok": True, "message": f"alert created for \"{topic}\" (delivered to your Gmail)"}
    raise AlertError("clicked Create Alert but the alert did not appear in My alerts")


def _evidence(page, prefix):
    try:
        Path(f"{prefix}.png").write_bytes(page.screenshot(full_page=True))
        Path(f"{prefix}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass
