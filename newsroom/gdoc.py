"""The Google Doc bridge: read the Newsroom Inbox and append status lines.

Two backends (setting inbox_source):
- "doc":  the Google Doc, through the Google-signed-in Chrome profile. Read = the doc's plain-text export (fetched with the
          profile's cookies, no page rendering); append = open the doc, go to the end, type the line.
- "file": a local text file with the same format (tests, or running without Google).
Appends that fail are queued in the state file and retried (they are never lost)."""
import re
import time
from pathlib import Path

from . import browser


class DocError(RuntimeError):
    pass


def _norm(text):
    """Compare lines the way Google Docs stores them: it turns ' and " into curly quotes (and - into dashes) as you type."""
    t = str(text).replace("\r", "")
    for a, b in (("\u2018", "'"), ("\u2019", "'"), ("\u201c", '"'), ("\u201d", '"'), ("\u2013", "-"), ("\u2014", "-"), ("\u00a0", " ")):
        t = t.replace(a, b)
    return t


def has_line(text, line):
    return _norm(line) in _norm(text)


def read(cfg):
    """-> the inbox text. Raises DocError with a plain reason."""
    if cfg.get("inbox_source") == "file":
        p = Path(cfg["inbox_file"])
        return p.read_text(encoding="utf-8") if p.exists() else ""
    if not cfg.get("doc_id"):
        raise DocError("no Google Doc set: put the Newsroom Inbox doc's ID in config.json (doc_id)")
    try:
        with browser.session(cfg) as (context, page):
            r = context.request.get(f"https://docs.google.com/document/d/{cfg['doc_id']}/export?format=txt", timeout=60000)
            body = r.text() if r.ok else ""
            if not r.ok or "<html" in body[:500].lower():
                if r.status in (401, 403) or "accounts.google.com" in (r.url or "") or "<html" in body[:500].lower():
                    raise DocError("the Newsroom Chrome profile is not signed in to Google (or can't open the doc): "
                                   "run scripts\\login-google.ps1")
                raise DocError(f"Google Docs answered HTTP {r.status}")
            return body
    except browser.NotSignedIn as e:
        raise DocError(str(e))
    except DocError:
        raise
    except Exception as e:
        raise DocError(f"couldn't read the doc ({str(e).splitlines()[0][:200]})")


def append(cfg, line):
    """Append one line at the end of the doc. Raises DocError."""
    line = re.sub(r"[\r\n]+", " ", line).strip()
    if cfg.get("inbox_source") == "file":
        p = Path(cfg["inbox_file"]); p.parent.mkdir(parents=True, exist_ok=True)
        text = p.read_text(encoding="utf-8") if p.exists() else ""
        p.write_text(text + ("" if not text or text.endswith("\n") else "\n") + line + "\n", encoding="utf-8")
        return
    if not cfg.get("doc_id"):
        raise DocError("no Google Doc set (doc_id)")
    if has_line(read(cfg), line):   # already there (an earlier try saved late): never write a line twice
        return
    first = cfg.get("doc_write_headless", cfg.get("headless", True))
    for k, hl in enumerate([first] + ([False] if first else [])):   # headless first; a visible window if typing didn't land
        try:
            if k and has_line(read(cfg), line):   # the first try saved late: don't write it twice
                return
            return _append_once(cfg, line, hl)
        except DocError as e:
            if k or not first or "not in the doc's text" not in str(e):
                raise


def _append_once(cfg, line, headless):
    evidence = Path(cfg.get("state_file", "newsroom_state.json")).parent / "doc_fail"
    try:
        with browser.session(cfg, headless=headless) as (context, page):
            try:
                page.goto(f"https://docs.google.com/document/d/{cfg['doc_id']}/edit", wait_until="domcontentloaded", timeout=60000)
                if browser.signed_out(page):
                    raise DocError("the Newsroom Chrome profile is not signed in to Google: run scripts\\login-google.ps1")
                page.wait_for_selector(".kix-appview-editor", timeout=45000)
                page.wait_for_timeout(3000)
                if re.search(r"(View only|Request edit access|You need permission)", page.locator("body").inner_text(timeout=5000), re.I):
                    raise DocError("the signed-in account can only view this doc (no edit access)")
                page.bring_to_front()
                page.locator(".kix-appview-editor").first.click(position={"x": 300, "y": 200}, timeout=10000)
                page.wait_for_timeout(500)
                # Docs reads keys from a hidden input iframe: give it the focus (a headless window has none by itself)
                page.evaluate("""() => { const f = document.querySelector('iframe.docs-texteventtarget-iframe');
                    const d = f && f.contentDocument; const t = d && (d.querySelector('[contenteditable=true]') || d.body);
                    if (f) f.focus(); if (t) t.focus(); }""")
                page.wait_for_timeout(300)
                page.keyboard.press("Control+End"); page.wait_for_timeout(500)
                page.keyboard.press("End")
                page.keyboard.press("Enter")
                page.keyboard.type(line, delay=8)   # real key events into Docs' input frame
                # Docs saves a moment after typing stops: wait for the save, then confirm from the doc's text
                page.wait_for_timeout(4000)
                t0 = time.time()
                while time.time() - t0 < 30:
                    body = page.locator("body").inner_text(timeout=3000)
                    if not re.search(r"Saving", body): break
                    page.wait_for_timeout(1500)
                page.wait_for_timeout(2000)
                export = context.request.get(f"https://docs.google.com/document/d/{cfg['doc_id']}/export?format=txt", timeout=60000).text()
                if not has_line(export, line):
                    raise DocError("the line was typed but is not in the doc's text (not saved?)")
            except Exception:
                try:
                    Path(f"{evidence}.png").write_bytes(page.screenshot(full_page=False))
                except Exception:
                    pass
                raise
    except browser.NotSignedIn as e:
        raise DocError(str(e))
    except DocError as e:
        raise DocError(f"{e} (screenshot {evidence}.png)")
    except Exception as e:
        raise DocError(f"couldn't write to the doc ({str(e).splitlines()[0][:200]}; screenshot {evidence}.png)")
