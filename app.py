"""The Newsroom: Google Alerts -> (Muse) -> Newsroom Inbox doc -> Video Studio -> staged video -> YouTube Publisher.
Local only (127.0.0.1:42040). Talks to Video Studio (42021) and the Publisher (42030) over HTTP; imports neither."""
import os
import sys
import threading
import time
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

sys.path.insert(0, str(Path(__file__).resolve().parent))
from newsroom import alerts, bridge, config, core, gdoc  # noqa: E402

ROOT = Path(__file__).resolve().parent
app = Flask(__name__, static_folder=str(ROOT / "static"))
cfg = config.load()
try:   # the phone passcode lives outside the repo: F:\videos\newsroom\mobile_pin.txt
    cfg.setdefault("mobile_pin", (Path(cfg["state_file"]).parent / "mobile_pin.txt").read_text(encoding="utf-8").strip() or None)
except (OSError, KeyError):
    pass
room = core.Newsroom(cfg)
_scripts = {}


def _phone_ok():
    """The phone page (/m) from another device on your network (or Tailscale): only with the passcode (mobile_pin)."""
    pin = str(cfg.get("mobile_pin") or "")
    if not pin: return False
    if request.args.get("k") == pin: return True
    return request.cookies.get("nk") == pin


def _home_net():
    """A private (home network) address: the laptop's setup endpoints are never served to anything else."""
    import ipaddress
    try: return ipaddress.ip_address(request.remote_addr).is_private
    except ValueError: return False


@app.before_request
def local_only():
    local = request.remote_addr in ("127.0.0.1", "::1") and request.host.split(":")[0] in ("127.0.0.1", "localhost")
    if not local:
        phone_paths = request.path in ("/m", "/api/staged", "/api/status", "/api/publish", "/api/reject", "/api/feedback") or request.path.startswith("/media/")
        laptop_paths = request.path.startswith("/laptop/") and _home_net()   # the laptop's one-shot setup (passcode too)
        if not ((phone_paths or laptop_paths) and _phone_ok()):
            return jsonify(error="Local access only (the phone page needs its passcode link)."), 403
    if request.method != "GET":   # a web page elsewhere must not be able to press our buttons
        origin = request.headers.get("Origin")
        if origin and origin != request.host_url.rstrip("/"):
            return jsonify(error="Cross-origin requests are not allowed."), 403


@app.after_request
def read_only_cors(resp):
    # read-only status may be read by the other local tools' pages (Video Studio's tab); nothing else
    if request.method == "GET" and request.path.startswith("/api/") and request.headers.get("Origin", "").startswith("http://127.0.0.1:"):
        resp.headers["Access-Control-Allow-Origin"] = request.headers["Origin"]
    return resp


@app.errorhandler(ValueError)
@app.errorhandler(bridge.BridgeError)
@app.errorhandler(gdoc.DocError)
def bad(e):
    return jsonify(error=str(e)), 400


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/m")
def phone():
    """Phone review page: staged videos only (watch, Publish, Reject). Open http://<PC address>:42040/m?k=<mobile_pin> once."""
    resp = send_from_directory(app.static_folder, "m.html")
    if request.args.get("k") and request.args.get("k") == str(cfg.get("mobile_pin") or ""):
        resp.set_cookie("nk", request.args["k"], max_age=180 * 86400, httponly=True, samesite="Strict")
    return resp


@app.get("/api/status")
def status():
    vs, pub = {}, {}
    try: vs = bridge.vs_status(cfg); vs = {"ok": True, "current": vs.get("current"), "wangp": vs.get("wangp")}
    except bridge.BridgeError as e: vs = {"ok": False, "error": str(e)}
    try: bridge.pub_health(cfg); pub = {"ok": True}
    except bridge.BridgeError as e: pub = {"ok": False, "error": str(e)}
    inbox = room.state["inbox"]
    return jsonify(video_studio=vs, publisher=pub, inbox_error=inbox.get("error", ""), inbox_at=inbox.get("at", 0),
                   inbox_source=cfg.get("inbox_source"), pending_writes=room.state["pending_writes"],
                   channels=cfg.get("channels"), mode=cfg.get("mode"), auto=cfg.get("auto_generate", True), counts=room.counts(), publish_queue=room.state.get("publish_queue", []), visibility=cfg.get("visibility"), log=room.state["log"][-25:])


@app.get("/api/inbox")
def get_inbox():
    return jsonify(room.inbox_items(int(request.args.get("offset", 0)), int(request.args.get("limit", 5))))


@app.post("/api/inbox/refresh")
def refresh():
    room.refresh_inbox()
    return jsonify(room.inbox_items(0, 5))


@app.post("/api/generate")
def generate():
    b = request.get_json(force=True) or {}
    return jsonify(room.generate(str(b.get("id", "")), b.get("mode") or None))


@app.get("/api/active")
def active():
    """The item being made (newest first) with Video Studio's progress and, once written, its script."""
    busy = [dict(v, id=k) for k, v in room.state["items"].items() if v.get("status") in ("researching", "rendering")] or \
           [dict(v, id=k) for k, v in room.state["items"].items() if v.get("status") in ("paused", "stalled")]
    held = [dict(id=k, headline=v.get("headline", ""), status=v.get("status"), job=v.get("job"), resume_req=bool(v.get("resume_req")),
                 worker="laptop" if v.get("worker") == "laptop" else "PC")
            for k, v in room.state["items"].items() if v.get("status") in ("paused", "stalled")]
    recent = sorted(room.state["items"].items(), key=lambda kv: kv[1].get("updated", 0), reverse=True)
    pick = sorted(busy, key=lambda v: v.get("created", 0), reverse=True)
    it = pick[0] if pick else (dict(recent[0][1], id=recent[0][0]) if recent else None)
    if it and it.get("job"):
        jid = it["job"]
        got, at = _scripts.get(jid, (None, 0))
        if not got and time.time() - at > 30:   # not written yet: ask again at most every 30 s (the laptop can be slow to answer)
            got = bridge.vs_script(room._vcfg(it), jid, "long" if it.get("mode") == "long" else "short")
            _scripts[jid] = (got, time.time())
        it["script"] = got
        try:
            j = bridge.vs_job(room._vcfg(it), jid)   # the laptop's own Video Studio for a laptop job
            it["job_log"] = j.get("log", [])[-12:]; it["stages"] = j.get("stages", []); it["done"] = j.get("done", [])
        except bridge.BridgeError as e:
            it["job_log"] = [f"Video Studio: {e}"]
    return jsonify(item=it, queue=len(busy), held=held)


@app.get("/api/staged")
def staged():
    return jsonify(items=room.staged(include_rejected=request.args.get("all") == "1"))


@app.get("/media/<iid>/<kind>")
def media(iid, kind):
    if kind not in ("short", "long"): abort(404)
    p = room.video_path(iid, kind)
    if not p: abort(404)
    return send_file(p, conditional=True)


@app.post("/api/publish")
def publish():
    b = request.get_json(force=True) or {}
    return jsonify(room.publish(str(b.get("id", "")), str(b.get("channel", "")).lower(), b.get("visibility"), b.get("kinds")))


@app.post("/api/explainer")
def explainer():
    """Make the explainer for a staged Short (queued; one at a time)."""
    b = request.get_json(force=True) or {}
    ids = b.get("ids") or [b.get("id", "")]
    return jsonify(items=[room.add_explainer(str(i)) for i in ids])


@app.post("/api/job/<action>")
def job_action(action):
    """Pause / Resume / Retry the video being made (never killed: a paused or stalled job keeps all its work)."""
    b = request.get_json(force=True) or {}
    fn = {"pause": room.pause, "resume": room.resume_job, "retry": room.retry}.get(action)
    if not fn: abort(404)
    return jsonify(item=fn(str(b.get("id", ""))))


@app.post("/api/explainer/cancel")
def explainer_cancel():
    b = request.get_json(force=True) or {}
    ids = b.get("ids") or [b.get("id", "")]
    return jsonify(items=[room.cancel_explainer(str(i)) for i in ids])


@app.post("/api/approve")
def approve():
    b = request.get_json(force=True) or {}
    return jsonify(item=room.approve(str(b.get("id", "")), bool(b.get("on", True)), b.get("channel"), b.get("visibility"), b.get("kinds")))


@app.post("/api/publish_approved")
def publish_approved():
    return jsonify(queue=room.publish_approved())


@app.post("/api/reject")
def reject():
    b = request.get_json(force=True) or {}
    return jsonify(room.reject(str(b.get("id", "")), str(b.get("reason", ""))))


@app.post("/api/resubmit")
def resubmit():
    b = request.get_json(force=True) or {}
    return jsonify(room.resubmit(str(b.get("id", "")), str(b.get("channel", "")).lower(), b.get("visibility"), b.get("kinds"), bool(b.get("move"))))


@app.post("/api/move/delete")
def move_delete():
    b = request.get_json(force=True) or {}
    return jsonify(room.move_delete(str(b.get("id", ""))))


@app.post("/api/move/cancel")
def move_cancel():
    b = request.get_json(force=True) or {}
    return jsonify(room.move_cancel(str(b.get("id", ""))))


@app.post("/api/feedback")
def feedback():
    b = request.get_json(force=True) or {}
    return jsonify(room.feedback(str(b.get("id", "")), str(b.get("note", ""))))


@app.get("/api/style")
def style_rules():
    return jsonify(rules=bridge.vs_style(cfg))


@app.post("/api/style")
def style_add():
    b = request.get_json(force=True) or {}
    return jsonify(bridge.vs_style_add(cfg, str(b.get("rule", "")), str(b.get("area", "general"))))


@app.delete("/api/style/<rid>")
def style_delete(rid):
    return jsonify(bridge.vs_style_delete(cfg, rid))


@app.post("/api/alerts")
def make_alert():
    b = request.get_json(force=True) or {}
    try:
        return jsonify(alerts.create(cfg, b.get("topic", ""), room.log))
    except Exception as e:   # alerts.create only raises AlertError; anything else is still a message, never a 500 page
        room.log(f"Google Alert failed: {e}")
        return jsonify(error=str(e)), 400


@app.post("/api/inbox/add")
def add_item():
    """Add an item by hand (the same [PENDING] line Muse writes)."""
    b = request.get_json(force=True) or {}
    return jsonify(room.add_pending(str(b.get("headline", "")), str(b.get("url", "")), str(b.get("pitch", ""))))


# ---- the Linux laptop's one-shot setup: curl -fsSL "http://<PC>:42040/laptop/setup.sh?k=<passcode>" | bash
@app.get("/laptop/setup.sh")
def laptop_setup():
    sh = Path(cfg.get("vs_repo") or r"F:\code\video-studio-test") / "scripts" / "linux" / "setup-worker.sh"
    if not sh.exists(): abort(404)
    body = sh.read_text(encoding="utf-8").replace("\r\n", "\n")
    pc = request.host   # tells the script where to report back
    body = body.replace("@@PC@@", pc).replace("@@PIN@@", request.args.get("k", ""))
    return app.response_class(body, mimetype="text/x-shellscript")


@app.get("/laptop/bundle")
def laptop_bundle():
    """The tarball (code + setup.sh) for a laptop without a GitHub sign-in: curl ... | tar xj && bash vs-laptop/setup.sh"""
    f = Path(cfg.get("laptop_bundle") or r"F:\code\_laptop\vs-laptop.tar.bz2")
    if not f.exists(): abort(404)
    return send_file(f, mimetype="application/x-bzip2", as_attachment=True, download_name="vs-laptop.tar.bz2")


@app.get("/laptop/music")
def laptop_music():
    """Video Studio's music library (library.json + hook/ + bed/ tracks) as a .tar.gz: the laptop's hourly vs-sync copies it."""
    from newsroom import laptop
    buf = laptop.music_tar(cfg)
    if buf is None: abort(404)
    return send_file(buf, mimetype="application/gzip", as_attachment=True, download_name="music.tgz")


@app.get("/laptop/keys")
def laptop_keys():
    """The laptop's DeepSeek key and Hugging Face token, from this PC (home network + passcode only). Never the Anthropic key."""
    from newsroom import laptop
    return jsonify(laptop.keys(cfg))


@app.post("/laptop/register")
def laptop_register():
    """The setup script's last step: the laptop says it is done (name, address, MAC, token); the Newsroom checks it can reach it."""
    from newsroom import laptop
    return jsonify(laptop.register(cfg, room, request.get_json(silent=True) or {}))


@app.errorhandler(Exception)
def crash(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    import traceback; traceback.print_exc()
    room.log(f"Server error on {request.path}: {type(e).__name__}: {str(e)[:200]}")
    return jsonify(error=f"{type(e).__name__}: {str(e)[:300]}"), 500


if __name__ == "__main__":
    from newsroom import single
    lock = Path(cfg["state_file"]).parent / "newsroom.lock"
    if not single.acquire(lock):
        print(f"Another Newsroom is already running (lock {lock}); this copy exits.", flush=True)
        sys.exit(3)
    room.resume()
    if not os.environ.get("NEWSROOM_NO_BACKGROUND"):
        room.background()
    port = int(os.environ.get("PORT") or cfg.get("port", 42040))
    print(f"Newsroom running on http://127.0.0.1:{port}", flush=True)
    host = "0.0.0.0" if cfg.get("mobile_pin") else "127.0.0.1"   # reachable from the phone only when a passcode is set
    app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)
