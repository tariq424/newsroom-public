"""Settings: config.json next to app.py (created with these defaults on first run)."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "port": 42040,
    "video_studio": "http://127.0.0.1:42021",        # the fork; only its HTTP API is used
    "publisher": "http://127.0.0.1:42030",
    "video_studio_jobs": "F:\\videos_test\\jobs",      # read-only: finished outputs are copied from here to staging
    "staging_dir": "F:\\videos\\newsroom\\staging",    # one folder per job (output/ + plan.json): the Publisher reads it
    "state_file": "F:\\videos\\newsroom\\newsroom_state.json",
    # Google Doc bridge: "doc" = the Newsroom Inbox Google Doc through the signed-in Chrome profile; "file" = a local text
    # file with the same line format (tests / offline)
    "inbox_source": "doc",
    "doc_id": "",
    "inbox_file": "F:\\videos\\newsroom\\inbox_mock.txt",
    "chrome_profile": "F:\\videos\\newsroom\\chrome_profile",   # Google-signed-in; scripts\\login-google.ps1 once
    "chrome_path": "",                                          # "" = installed Chrome (channel "chrome")
    "headless": True,
    "inbox_refresh_minutes": 60,
    "auto_generate": True,           # new [PENDING] items are made automatically (one at a time, oldest first)
    "auto_retries": 1,               # a failed item is retried this many times (not when its source can't be read)
    "ignore_keywords": [],          # inbox items whose headline contains any of these are hidden (case-insensitive)
    # what "Generate Video" asks Video Studio for
    "mode": "short",                 # short | long | both
    "minutes": 15,                   # explainer length when mode is long / both
    "short_secs": 165,
    "audience": "beginner",
    "channels": ["yousocial", "moonspots", "youtour"],
    "visibility": "Private",
}


def path():
    return ROOT / "config.json"


def load():
    cfg = dict(DEFAULTS)
    p = path()
    if p.exists():
        try:
            cfg.update(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    else:
        try:
            p.write_text(json.dumps(DEFAULTS, indent=2), encoding="utf-8")
        except OSError:
            pass
    return cfg
