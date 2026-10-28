"""The Newsroom Inbox line format (written by Muse, appended to by the Newsroom):

    [PENDING] <Headline> | <Source URL> | <2-sentence pitch>
    [PENDING] <Headline> | <Source URL> | <pitch> | RESEARCH BRIEF: <background, ~300 words>   (optional 4th field)
    [PENDING] [BREAKING] <Headline> | ...   (optional: made before routine items)
    [PROCESSED] <Headline> - <Job ID>
    [PUBLISHED] <Headline> - <YouTube Link>
    [REJECTED] <Headline> - <Job ID>   (you pressed Reject: Muse removes the story's lines, like a published one)

An item is active while no [PROCESSED] / [PUBLISHED] line names the same headline."""
import hashlib
import re
from urllib.parse import parse_qs, urlparse

PENDING = re.compile(r"^\s*\[PENDING\]\s*(?P<head>.+?)\s*\|\s*(?P<url>https?://[^\s/|]+\.[^\s|]+)\s*(?:\|\s*(?P<pitch>.*))?$", re.I)
DONE = re.compile(r"^\s*\[(?P<tag>PROCESSED|PUBLISHED|REJECTED)\]\s*(?P<head>.+?)\s+-\s+(?P<ref>\S+)\s*$", re.I)


BRIEF = re.compile(r"\s*\|\s*RESEARCH BRIEF\s*:\s*", re.I)
BREAKING = re.compile(r"^\s*\[BREAKING\]\s*", re.I)


def norm(headline):
    return re.sub(r"[^a-z0-9]+", " ", str(headline).lower()).strip()


def item_id(headline):
    return hashlib.sha1(norm(headline).encode()).hexdigest()[:12]


def unwrap(url):
    """Google Alerts links are redirects (google.com/url?...&url=<article>): return the article URL."""
    u = urlparse(url)
    if u.netloc.endswith("google.com") and u.path == "/url":
        q = parse_qs(u.query)
        for k in ("url", "q"):
            if q.get(k):
                return q[k][0]
    return url


def parse(text):
    """-> (pending [newest first], done {norm(headline): {"processed": job, "published": link}})"""
    pending, done = [], {}
    for n, line in enumerate(str(text or "").splitlines()):
        line = line.strip().lstrip("﻿")
        m = DONE.match(line)
        if m:
            d = done.setdefault(norm(m["head"]), {})
            d[m["tag"].lower()] = m["ref"]
            continue
        m = PENDING.match(line)
        if m:
            head = m["head"].strip(); breaking = bool(BREAKING.match(head)); head = BREAKING.sub("", head)
            parts = BRIEF.split(m["pitch"] or "", maxsplit=1)
            if len(parts) == 1:   # an unlabelled 4th field: "| pitch | brief"
                parts = re.split(r"\s+\|\s+", m["pitch"] or "", maxsplit=1)
            pitch, brief = parts[0], (parts[1] if len(parts) > 1 else "")
            pending.append({"id": item_id(head), "headline": head, "url": unwrap(m["url"].strip()),
                            "pitch": pitch.strip(), "brief": brief.strip(), "breaking": breaking, "line": n})
    pending.reverse()   # the doc is appended at the end: newest last -> newest first
    seen, out = set(), []
    for it in pending:   # the same headline twice: keep the newest
        if it["id"] not in seen:
            seen.add(it["id"]); out.append(it)
    return out, done


def active(text):
    """Pending items with no PROCESSED / PUBLISHED line yet, newest first."""
    pending, done = parse(text)
    return [it for it in pending if norm(it["headline"]) not in done]


def processed_line(headline, job_id):
    return f"[PROCESSED] {clean(headline)} - {job_id}"


def rejected_line(headline, ref):
    return f"[REJECTED] {clean(headline)} - {ref}"


def published_line(headline, link):
    return f"[PUBLISHED] {clean(headline)} - {link}"


def clean(headline):
    return re.sub(r"\s+", " ", str(headline)).replace("|", "/").strip()
