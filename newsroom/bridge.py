"""HTTP bridges to Video Studio (42021) and the YouTube Publisher (42030). No code of either is imported."""
import requests


class BridgeError(RuntimeError):
    pass


TOKENS = {}   # base URL -> token (the Linux laptop's Video Studio requires one)


def _hdr(url):
    return {"X-VS-Token": t for base, t in TOKENS.items() if url.startswith(base)}


def _get(url, timeout=15):
    try:
        r = requests.get(url, timeout=timeout, headers=_hdr(url))
    except requests.RequestException as e:
        raise BridgeError(f"{url.split('/api')[0].split('/publish')[0]} is not answering ({str(e)[:100]})")
    if r.status_code >= 400:
        raise BridgeError(_err(r))
    return r.json()


def _post(url, body, timeout=30):
    try:
        r = requests.post(url, json=body, timeout=timeout, headers=_hdr(url))
    except requests.RequestException as e:
        raise BridgeError(f"{url.split('/api')[0].split('/publish')[0]} is not answering ({str(e)[:100]})")
    if r.status_code >= 400:
        raise BridgeError(_err(r))
    return r.json()


def _err(r):
    try: return str(r.json().get("error") or r.text)[:300]
    except ValueError: return f"HTTP {r.status_code}: {r.text[:200]}"


# ---------------------------------------------------------------- Video Studio
def vs_status(cfg):
    return _get(cfg["video_studio"] + "/api/status")


def vs_start(cfg, source, headline, mode=None):
    """Start a Video Studio job for a researched source. -> job id."""
    mode = mode or cfg.get("mode", "short")   # "smoke" = Video Studio's 30-second test Short
    body = {"mode": mode, "audience": cfg.get("audience", "beginner"), "auto_approve": True,   # skip script review
            "visual_review": "never", "captions": True, "minutes": cfg.get("minutes", 8), "short_secs": cfg.get("short_secs", 165),
            "src_title": headline[:200]}
    if source["kind"] == "youtube":
        body["url"] = source["url"]
    else:
        body.update(url="", transcript=source["text"], src_credit=source.get("site") or "")
    r = _post(cfg["video_studio"] + "/api/jobs", body)
    if not r.get("id"):
        raise BridgeError(f"Video Studio didn't return a job id ({r})")
    return r["id"]


def vs_pause(cfg, jid):
    """Video Studio's 'Pause now': the job stops at its next safe point (never inside a render) and keeps all its work."""
    return _post(f"{cfg['video_studio']}/api/jobs/{jid}/pause", {})


def vs_settings(cfg, changes):
    """Change Video Studio settings (used to keep the laptop's in line with what laptop jobs need)."""
    return _post(f"{cfg['video_studio']}/api/settings", changes)


def vs_resume(cfg, jid):
    return _post(f"{cfg['video_studio']}/api/jobs/{jid}/resume", {})


def vs_outputs(cfg, jid):
    return _get(f"{cfg['video_studio']}/api/jobs/{jid}/outputs")["files"]


def vs_download(cfg, jid, rel, dest):
    url = f"{cfg['video_studio']}/files/{jid}/{rel}"
    with requests.get(url, stream=True, timeout=60, headers=_hdr(url)) as r:
        if r.status_code >= 400: raise BridgeError(_err(r))
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20): f.write(chunk)


def vs_job(cfg, jid):
    return _get(f"{cfg['video_studio']}/api/jobs/{jid}")


def vs_script(cfg, jid, kind="short"):
    """The job's generated script: [{"id", "title", "vo"}] (Short beats, or explainer sections)."""
    for path in (("short/plan.json", "plan.json") if kind != "long" else ("plan.json", "short/plan.json")):
        try:
            u = f"{cfg['video_studio']}/files/{jid}/{path}"
            r = requests.get(u, timeout=15, headers=_hdr(u))   # the laptop's Video Studio needs its token
            if r.ok:
                plan = r.json()
                return {"title": plan.get("title", ""), "kind": "short" if path.startswith("short") else "long",
                        "beats": [{"id": s.get("id"), "title": s.get("title", ""), "vo": s.get("vo", "")} for s in plan.get("sections", [])]}
        except (requests.RequestException, ValueError):
            continue
    return None


# ---------------------------------------------------------------- Publisher
def pub_health(cfg):
    return _get(cfg["publisher"] + "/health", timeout=8)


def pub_channels(cfg):
    return cfg.get("channels") or []


def pub_publish(cfg, job, kinds, channel, visibility, mode="new"):
    """Publish a staged job. kinds: ["short"], ["long"] or ["long", "short"] (pair: explainer first, its link goes into
    the Short). -> {"tasks": {kind: task_id}}"""
    drafts = {}
    for kind in kinds:
        d = _get(f"{cfg['publisher']}/api/defaults/{job}/{kind}/{channel}")["data"]
        d["visibility"] = visibility
        drafts[kind] = d
    if len(kinds) == 2:
        r = _post(cfg["publisher"] + "/publish/pair", {"long": drafts["long"], "short": drafts["short"]})
        return {"tasks": {"long": r["long_task"], "short": r["short_task"]}}
    r = _post(cfg["publisher"] + "/publish", {**drafts[kinds[0]], "mode": mode})   # "resume": finish the existing upload
    return {"tasks": {kinds[0]: r["task_id"]}}


def pub_task(cfg, task_id):
    return _get(f"{cfg['publisher']}/publish/status/{task_id}")


def pub_delete(cfg, channel, job, kind, video_id):
    """Ask the Publisher to delete ONE already-uploaded video (the second half of a Newsroom move).
    Contract (MSG 030): POST {publisher}/delete {"channel", "job", "kind", "video_id"} -> {"deleted": true, "video_id"}.
    The Publisher finds its record for that job/kind on that channel, deletes the video in YouTube Studio,
    and marks the record deleted. Idempotent: an already-deleted (or already-gone) video still returns deleted."""
    return _post(cfg["publisher"] + "/delete", {"channel": channel, "job": job, "kind": kind, "video_id": video_id}, timeout=180)


def _delete(url, timeout=15):
    try:
        r = requests.delete(url, timeout=timeout, headers=_hdr(url))
    except requests.RequestException as e:
        raise BridgeError(f"{url.split('/api')[0]} is not answering ({str(e)[:100]})")
    if r.status_code >= 400: raise BridgeError(_err(r))
    return r.json()


# ---- house style learned from feedback (Video Studio studio/style.py)
def vs_style(cfg):
    return _get(cfg["video_studio"] + "/api/style").get("rules", [])


def vs_style_learn(cfg, note, context, job=""):
    return _post(cfg["video_studio"] + "/api/style/learn", {"note": note, "context": context, "job": job}, timeout=120)


def vs_style_add(cfg, rule, area):
    return _post(cfg["video_studio"] + "/api/style", {"rule": rule, "area": area})


def vs_style_delete(cfg, rid):
    return _delete(cfg["video_studio"] + f"/api/style/{rid}")


def vs_style_import(cfg, rules):
    return _post(cfg["video_studio"] + "/api/style/import", {"rules": rules})
