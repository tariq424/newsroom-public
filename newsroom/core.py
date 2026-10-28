"""The Newsroom's own state and background work: inbox cache, Generate -> research -> Video Studio job -> staging ->
[PROCESSED], Publish -> Publisher -> [PUBLISHED]. Everything survives a restart (state file); jobs that were rendering are
picked up again on start."""
import json
import re
import shutil
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import urlparse

from . import bridge, gdoc, inbox, laptop, research

_lock = threading.RLock()
_start_lock = threading.Lock()
BLOCKING = ("researching", "rendering")   # one video made at a time; videos waiting in Staging don't hold the queue
UNREVIEWED = ("staged", "publish_failed")    # waiting for your review (counted, never a reason to stop production)
_flush = threading.Lock()   # one doc writer at a time (the background retry and a new line must not both write it)
_threads = {}
LOG_MAX = 200


def _on_laptop(v):
    return (v or {}).get("worker") == "laptop"


def _video_id(url):
    """The YouTube video id inside a watch / youtu.be / shorts / live URL ('' when there is none)."""
    u = str(url or "")
    for pat in ("watch?v=", "youtu.be/", "/shorts/", "/live/"):
        if pat in u: return u.split(pat, 1)[1].split("&")[0].split("?")[0].split("/")[0]
    return ""


def ai_visuals(job_dir):
    """Rung tags of AI-generated visuals used in the job's render timelines (Gemini, Flow, WanGP...)."""
    found = []
    for t in (Path(job_dir) / "short" / "public" / "teaser.json", Path(job_dir) / "build" / "public" / "teaser.json"):
        try: data = json.loads(t.read_text(encoding="utf-8"))
        except (OSError, ValueError): continue
        for beat in data.get("beats", []):
            for bg in beat.get("bgs", []):
                tag = str(bg.get("tag") or "")
                if ("GENERATED" in tag.upper() or "FLOW" in tag.upper() or "GEMINI" in tag.upper()) and tag not in found:
                    found.append(tag)
    return found


class Newsroom:
    def __init__(self, cfg):
        self.cfg = cfg
        self.state_path = Path(cfg["state_file"])
        self.state = self._load()
        self.state.setdefault("items", {}); self.state.setdefault("pending_writes", []); self.state.setdefault("log", [])
        self.state.setdefault("inbox", {"text": "", "at": 0, "error": ""})
        self.stop = threading.Event()
        self._import_state()

    def _import_state(self):
        """state_import.json next to the state file (e.g. a backup copy): items this state doesn't know are added back
        (staged / published videos keep their Staging cards). The file is renamed after use."""
        imp = self.state_path.with_name("state_import.json")
        if not imp.is_file(): return
        try: old = json.loads(imp.read_text(encoding="utf-8"))
        except (OSError, ValueError): return
        added = [k for k in (old.get("items") or {}) if k not in self.state["items"]]
        for k in added: self.state["items"][k] = old["items"][k]
        for k in ("starts", "last_start"):
            if k in old and k not in self.state: self.state[k] = old[k]
        self.save()
        imp.replace(imp.with_name(f"state_import.used-{int(time.time())}.json"))
        print(f"Imported {len(added)} item(s) from {imp.name}", flush=True)

    # ------------------------------------------------------------ state
    def _load(self):
        try: return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError): return {}

    def save(self):
        with _lock:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, indent=1, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.state_path)

    def log(self, msg):
        line = time.strftime("%H:%M:%S ") + msg
        print(line, flush=True)
        with _lock:
            self.state["log"] = (self.state["log"] + [line])[-LOG_MAX:]
        self.save()

    def item(self, iid):
        return self.state["items"].get(iid)

    def _set(self, iid, **kw):
        with _lock:
            self.state["items"].setdefault(iid, {}).update(kw, updated=time.time())
        self.save()

    # ------------------------------------------------------------ inbox
    def refresh_inbox(self):
        try:
            text = gdoc.read(self.cfg)
            with _lock: self.state["inbox"] = {"text": text, "at": time.time(), "error": ""}
        except gdoc.DocError as e:
            with _lock: self.state["inbox"].update(error=str(e), at=time.time())
            self.log(f"Inbox: {e}")
        self.save()
        return self.state["inbox"]

    def inbox_items(self, offset=0, limit=5):
        """Active [PENDING] items (no PROCESSED / PUBLISHED line), newest first, with the Newsroom's local status."""
        items = inbox.active(self.state["inbox"].get("text", ""))
        for it in items:
            loc = self.item(it["id"]) or {}
            it["status"] = loc.get("status", "new"); it["job"] = loc.get("job"); it["error"] = loc.get("error", "")
        hidden = {"staged", "published", "publishing", "paused", "stalled"}   # done here but the doc line may still be queued
        items = [it for it in items if it["status"] not in hidden]
        skip = [k.lower() for k in self.cfg.get("ignore_keywords") or [] if str(k).strip()]
        if skip:   # topics you don't want right now: hidden from the inbox (the doc is left as it is)
            items = [it for it in items if not any(k in it["headline"].lower() for k in skip)]
        return {"items": items[offset:offset + limit], "total": len(items), "offset": offset,
                "error": self.state["inbox"].get("error", ""), "at": self.state["inbox"].get("at", 0)}

    def add_pending(self, headline, url, pitch=""):
        """Append a [PENDING] line by hand (same format as Muse), then re-read the inbox."""
        headline = inbox.clean(headline); pitch = inbox.clean(pitch)
        if not 3 <= len(headline) <= 200: raise ValueError("headline: 3-200 characters")
        if not re.match(r"^https?://\S+$", url.strip()): raise ValueError("source URL must start with http:// or https://")
        line = f"[PENDING] {headline} | {url.strip()} | {pitch or 'Added by hand in the Newsroom.'}"
        gdoc.append(self.cfg, line)
        self.log(f"Inbox: added {headline[:80]}")
        self.refresh_inbox()
        return self.inbox_items(0, 5)

    # ------------------------------------------------------------ doc writes (queued, retried)
    def queue_line(self, line):
        with _lock:
            if line not in self.state["pending_writes"]: self.state["pending_writes"].append(line)
        self.save()
        self.flush_writes()

    def flush_writes(self):
        if not _flush.acquire(blocking=False):
            return False   # another flush is writing; it picks up anything queued meanwhile
        try:
            return self._flush_writes()
        finally:
            _flush.release()

    def _flush_writes(self):
        while True:
            with _lock: todo = [l for l in self.state["pending_writes"]]
            if not todo: return True
            if not self._write_some(todo): return False

    def _write_some(self, todo):
        for line in todo:
            try:
                gdoc.append(self.cfg, line)
            except gdoc.DocError as e:
                self.log(f"Doc write waiting (will retry): {line[:80]} ({e})")
                return False
            with _lock:
                if line in self.state["pending_writes"]: self.state["pending_writes"].remove(line)
            self.log(f"Doc: appended {line[:100]}")
        self.save()
        return True

    # ------------------------------------------------------------ generate
    def generate(self, iid, mode=None):
        with _start_lock:   # one start at a time, even when the button and the automatic queue fire together
            return self._generate(iid, mode)

    def _generate(self, iid, mode=None):
        if any((v.get("status") in BLOCKING and not _on_laptop(v)) or v.get("long_status") in ("researching", "rendering") for v in self.state["items"].values()):
            raise ValueError("a video is already being made: one at a time")
        try: cur = bridge.vs_status(self.cfg).get("current")
        except bridge.BridgeError as e: raise ValueError(f"Video Studio isn't answering ({e})")
        if cur: raise ValueError(f"Video Studio is busy with job {cur}: one at a time")
        it = next((x for x in inbox.active(self.state["inbox"].get("text", "")) if x["id"] == iid), None)
        if not it: raise ValueError("that inbox item is no longer pending (refresh the inbox)")
        loc = self.item(iid) or {}
        if loc.get("status") in ("researching", "queued", "rendering"):
            raise ValueError("this item is already being made")
        mode = mode or self.cfg.get("mode", "short")
        if mode not in ("smoke", "short", "long", "both"): raise ValueError("mode must be smoke, short, long or both")
        self._set(iid, headline=it["headline"], url=it["url"], pitch=it["pitch"], brief=it.get("brief", ""), breaking=bool(it.get("breaking")), status="researching", mode=mode, error="",
                  job=None, created=time.time())
        self._spawn(iid, self._run_generate, iid)
        return self.item(iid)

    def _spawn(self, key, fn, *a):
        t = threading.Thread(target=self._guard, args=(key, fn) + a, daemon=True); _threads[key] = t; t.start()

    def _guard(self, key, fn, *a):
        try: fn(*a)
        except Exception as e:
            self.log(f"{key}: {type(e).__name__}: {e}"); traceback.print_exc()
            iid = a[0] if a else key
            if self.item(iid): self._set(iid, status="failed", error=str(e)[:400])

    def _run_generate(self, iid):
        it = self.item(iid)
        src = self._research(iid)
        if not src: return
        jid = bridge.vs_start(self.cfg, src, it["headline"], it.get("mode"))
        self._set(iid, status="rendering", job=jid, source_kind=src["kind"], source_words=src.get("words", 0))
        self.log(f"Video Studio job {jid} started for: {it['headline'][:80]}")
        self._watch(iid)

    def _research(self, iid, fail=True):
        """Read the item's source (+ Muse's research brief). -> source dict, or None (item marked failed when fail)."""
        it = self.item(iid)
        self.log(f"Researching: {it['headline'][:80]} ({it['url']})")
        brief = (it.get("brief") or "").strip()
        try:
            src = research.fetch(it["url"], self.cfg, self.log)
        except RuntimeError as e:
            src = self._alternate(iid, it, e)   # the same story from another outlet (the original blocks robots / paywall)
            if src is None:
                if len(brief.split()) < 150:
                    if fail: self._set(iid, status="failed", error=str(e) + "; no other outlet with the same story was readable")
                    self.log(f"Research failed: {e}"); return None
                self.log(f"  {e}; using Muse's research brief instead")
                src = {"kind": "article", "url": it["url"], "title": it["headline"], "site": "", "text": "", "words": 0}
        self.log(f"  source: {src['kind']}" + (f", {src['words']} words from {src['site']}" if src["kind"] == "article" else ""))
        if brief and src["kind"] == "article":   # Muse's extra context for short articles: clearly marked as background
            src = dict(src, text=(src["text"] + "\n\nBackground research (context gathered by the Newsroom's researcher, "
                                  "not from the source article):\n" + brief).strip())
            src["words"] = len(src["text"].split())
            self.log(f"  + Muse's research brief ({len(brief.split())} words) -> {src['words']} words in total")
        return src

    def _alternate(self, iid, it, err):
        """The original couldn't be read: try the same story from up to 4 other outlets (Bing News). -> source dict or None."""
        host = urlparse(it["url"]).hostname or ""
        alts = research.alternates(it["headline"], host, limit=4)
        if not alts: self.log(f"  {err}; no other outlet found for this story"); return None
        self.log(f"  {err}; trying the same story elsewhere: {', '.join(s or urlparse(u).hostname for u, _t, s in alts)}")
        for u, _t, s in alts:
            try:
                src = research.fetch(u, self.cfg, self.log)   # quick read, then the real browser for script-built pages (MSN)
                self._set(iid, source_alt=u)
                self.log(f"  using {src['site']} instead ({src['words']} words): {u}")
                return src
            except RuntimeError as e2:
                self.log(f"    {s or urlparse(u).hostname}: {str(e2)[:100]}")
        return None

    def _watch(self, iid):
        """Poll the Video Studio job until it finishes, then stage it and queue the [PROCESSED] line."""
        it = self.item(iid); jid = it["job"]; misses = 0
        while not self.stop.is_set():
            try:
                j = bridge.vs_job(self._vcfg(self.item(iid)), jid); misses = 0
            except bridge.BridgeError as e:
                misses += 1
                if misses in (1, 20): self.log(f"{jid}: Video Studio not answering ({e}); still waiting")
                if _on_laptop(self.item(iid)) and misses % 4 == 0:   # the laptop's address may have changed (or it's asleep)
                    base, _st = laptop.find(self.cfg, self.state)
                    if base: self._set(iid, worker_url=base)
                self.stop.wait(30); continue
            st = j.get("status")
            self._set(iid, vs_status=st, vs_stage=j.get("stage"), vs_prog=j.get("prog"), vs_title=j.get("title", ""))
            if st == "finished":
                self._stage(iid, jid); return
            if st == "paused":   # paused (Pause button, or in Video Studio): kept, resumed when the pipeline is idle
                if self.item(iid).get("status") != "paused":
                    self._set(iid, status="paused", paused_at=time.time()); self.log(f"{jid}: paused (all work kept)")
                self.stop.wait(30); continue
            if st in ("error", "stopped"):   # crashed / interrupted / stopped: never thrown away; Resume or Retry by hand
                self._set(iid, status="stalled", error=f"Video Studio job {jid} {st}: {j.get('error', '')[:300]} (Resume continues it; Retry starts over)")
                self.log(f"{jid}: stalled ({st}); kept for Resume / Retry")
                if _on_laptop(self.item(iid)) and "WanGP" in str(j.get("error", "")) and not self.item(iid).get("wangp_retried"):
                    # the laptop has no WanGP: its settings are corrected before the resume (visuals drop to the next rung)
                    self._set(iid, resume_req=True, wangp_retried=True)
                    self.log(f"{jid}: the laptop has no WanGP; resuming with the fallback visuals as soon as the laptop is free")
                return
            if self.item(iid).get("status") == "paused":   # resumed from Video Studio itself
                self._set(iid, status="rendering")
            self.stop.wait(15)

    def _vcfg(self, it):
        """Video Studio settings for this item: the PC's, or the laptop's address."""
        if _on_laptop(it) and it.get("worker_url"):
            # the laptop's token, also after a Newsroom restart (the watch resumes before anything else has found the laptop)
            if it["worker_url"] not in bridge.TOKENS and laptop.token(self.cfg): bridge.TOKENS[it["worker_url"]] = laptop.token(self.cfg)
            return dict(self.cfg, video_studio=it["worker_url"])
        return self.cfg

    def _pull(self, it, jid):
        """Copy a finished laptop job into a local folder (same layout as a PC job). -> that folder."""
        dst = Path(self.cfg["staging_dir"]) / "_laptop" / jid
        vc = self._vcfg(it)
        for rel in bridge.vs_outputs(vc, jid):
            bridge.vs_download(vc, jid, rel, dst / rel)
        self.log(f"{jid}: copied back from the laptop")
        return dst

    def _stage(self, iid, jid):
        it0 = self.item(iid)
        src = self._pull(it0, jid) if _on_laptop(it0) else Path(self.cfg["video_studio_jobs"]) / jid
        dst = Path(self.cfg["staging_dir"]) / jid
        (dst / "output").mkdir(parents=True, exist_ok=True)
        kinds = []
        for f in (src / "output").iterdir() if (src / "output").is_dir() else []:
            if f.is_file() and not f.name.startswith("_"):
                shutil.copy2(f, dst / "output" / f.name)
                if f.name.endswith("_short.mp4"): kinds.append("short")
                elif "_explainer_" in f.name and f.suffix == ".mp4": kinds.append("long")
        for rel in ("plan.json", "short/plan.json", "state.json"):
            if (src / rel).is_file():
                (dst / rel).parent.mkdir(parents=True, exist_ok=True); shutil.copy2(src / rel, dst / rel)
        if not kinds:
            self._set(iid, status="failed", error=f"job {jid} finished but no video was found in {src / 'output'}"); return
        ai = ai_visuals(src)   # for YouTube's "Altered content" answer (the Publisher reads this file)
        (dst / "ai_content.json").write_text(json.dumps({"altered": bool(ai), "why": ai}, indent=1), encoding="utf-8")
        it = self.item(iid)
        note = ""   # a ticked presenter that couldn't be made: say why on the staged card (the Short went out without it)
        try:
            th = json.loads((src / "short" / "talkinghead" / "talkinghead.json").read_text(encoding="utf-8"))
            if not th.get("ok"): note = "skipped: " + str(th.get("reason") or th.get("error") or "unknown")[:200]
        except (OSError, ValueError): pass
        self._set(iid, status="staged", staged=str(dst), kinds=sorted(kinds, key=lambda k: k != "long"), staged_at=time.time(), presenter_note=note)
        self.log(f"Staged {jid}: {', '.join(kinds)} -> {dst}")
        self.queue_line(inbox.processed_line(it["headline"], jid))
        if it.get("want_long") and "long" not in kinds:   # a laptop Short: its explainer is made on the PC
            try: self.add_explainer(iid)
            except ValueError as e: self.log(f"  explainer not queued ({e})")

    # ------------------------------------------------------------ explainer for a video staged as a Short only
    def add_explainer(self, iid):
        """Queue an explainer for a staged Short-only item. Made one at a time, only while nothing else renders; it lands
        in the same staging folder, so Publish can do explainer first, then the Short with the explainer's link."""
        it = self.item(iid)
        if not it or it.get("status") not in ("staged", "publish_failed", "withheld"): raise ValueError("this item isn't staged")
        if "long" in (it.get("kinds") or []): raise ValueError("this item already has an explainer")
        if it.get("long_status") in ("queued", "researching", "rendering"): raise ValueError("its explainer is already queued")
        self._set(iid, long_status="queued", long_error="", long_queued=time.time())
        self.log(f"Explainer queued for {it.get('job')}: {it.get('headline', '')[:80]}")
        return self.item(iid)

    def pause(self, iid):
        it = self.item(iid)
        if not it or it.get("status") != "rendering" or not it.get("job"): raise ValueError("only a video being rendered can be paused")
        bridge.vs_pause(self._vcfg(it), it["job"])
        self._set(iid, pause_req=True); self.log(f"{it['job']}: pause requested (stops at the next safe point; nothing is lost)")
        return self.item(iid)

    def resume_job(self, iid, auto=False):
        """Continue a paused or stalled job in Video Studio (one at a time; paused videos don't count toward the daily cap)."""
        with _start_lock:
            it = self.item(iid)
            if not it or it.get("status") not in ("paused", "stalled") or not it.get("job"): raise ValueError("nothing paused or stalled to resume")
            mine = lambda v: _on_laptop(v) == _on_laptop(it)   # one job per machine
            where = "the laptop" if _on_laptop(it) else "the PC"
            def later(why):   # busy: queue it; it resumes as soon as that machine is free (before any new story)
                self._set(iid, resume_req=True)
                self.log(f"{it['job']}: resume queued ({why}); it continues as soon as {where} is free")
                return self.item(iid)
            if any(v.get("status") in BLOCKING and mine(v) for v in self.state["items"].values()):
                if auto: raise ValueError(f"{where} is busy")
                return later(f"{where} is making another video")
            try: cur = bridge.vs_status(self._vcfg(it)).get("current")
            except bridge.BridgeError as e: raise ValueError(f"Video Studio isn't answering ({e})")
            if cur:
                if auto: raise ValueError(f"Video Studio on {where} is busy with job {cur}")
                return later(f"Video Studio on {where} is busy")
            if _on_laptop(it): self._laptop_prep(it)
            bridge.vs_resume(self._vcfg(it), it["job"])
            self._set(iid, status="rendering", error="", pause_req=False, resume_req=False)
            self.log(f"{it['job']}: resumed{' automatically (pipeline idle)' if auto else ''}")
            self._spawn(iid, self._watch, iid)
            return self.item(iid)

    def retry(self, iid):
        """Start a stalled (or failed) item over as a new Video Studio job. The old job's files stay on disk."""
        it = self.item(iid)
        if not it or it.get("status") not in ("stalled", "failed"): raise ValueError("only a stalled or failed item can be retried")
        self._set(iid, status="new")
        try: return self.generate(iid)
        except ValueError: self._set(iid, status=it["status"]); raise

    def cancel_explainer(self, iid):
        """Drop a queued explainer (one already rendering is left to finish: stop it in Video Studio if needed)."""
        it = self.item(iid)
        if not it or it.get("long_status") != "queued": raise ValueError("no queued explainer for this item")
        self._set(iid, long_status="cancelled", long_error="")
        self.log(f"Explainer dropped for {it.get('job')}: {it.get('headline', '')[:80]}")
        return self.item(iid)

    def held(self):
        """A file named "hold" next to the state file pauses everything new (inbox items, explainers, auto-resumes),
        e.g. until Video Studio has been restarted with new code. Running jobs are not touched."""
        return (Path(self.cfg["state_file"]).parent / "hold").exists()

    def _today(self):
        t = time.localtime(); return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))   # local midnight

    def _breaking_preempt(self, busy):
        """A [BREAKING] item waiting while an ordinary video renders: pause that video (it resumes automatically after)."""
        if self.capped(breaking=True): return
        brk = [i for i in self.inbox_items(0, 10 ** 6)["items"] if i.get("breaking") and (self.item(i["id"]) or {}).get("status", "new") == "new"]
        if not brk: return
        for k, v in busy:
            if v.get("status") == "rendering" and not v.get("pause_req") and not v.get("breaking") and v.get("job"):
                try:
                    self.pause(k); self._set(k, pause_reason="breaking news")
                    self.log(f"BREAKING: {brk[0]['headline'][:60]} waits; pausing {v['job']} (resumes automatically after)")
                except (ValueError, bridge.BridgeError) as e:
                    self.log(f"BREAKING: couldn't pause {v.get('job')} ({e})")

    def capped(self, breaking=False):
        """New videos only (paused ones resuming are exempt): at most one per min_minutes_between_videos (60) and
        max_videos_per_day (10) in any rolling 24 hours. Videos waiting in Staging never stop production."""
        gap = float(self.cfg.get("min_minutes_between_videos", 60)) * 60
        if not breaking and time.time() - float(self.state.get("last_start", 0) or 0) < gap: return "one new video per hour"
        if len([t for t in self.state.get("starts", []) if t > time.time() - 86400]) >= int(self.cfg.get("max_videos_per_day", 10)):
            return "10 videos in the last 24 hours"
        return ""

    def _started(self):
        now = time.time()
        self.state["last_start"] = now
        self.state["starts"] = [t for t in self.state.get("starts", []) if now - t < 3 * 86400] + [now]
        self.save()

    def explainer_next(self):
        """Start the oldest queued explainer when Video Studio and the Newsroom are idle. -> item id or None."""
        if self.held() or self.capped(): return None
        items = self.state["items"]
        if any((v.get("status") in ("researching", "rendering") and not _on_laptop(v)) or v.get("long_status") in ("researching", "rendering")
               for v in items.values()): return None
        queued = sorted(((k, v) for k, v in items.items() if v.get("long_status") == "queued"), key=lambda kv: kv[1].get("long_queued", 0))
        if not queued: return None
        try:
            if bridge.vs_status(self.cfg).get("current"): return None   # Video Studio busy with something else
        except bridge.BridgeError: return None
        iid = queued[0][0]
        self._set(iid, long_status="researching"); self._started()
        self._spawn("long_" + iid, self._run_explainer, iid)
        return iid

    def _run_explainer(self, iid):
        try:
            it = self.item(iid)
            src = self._research(iid, fail=False)
            if not src: raise RuntimeError("couldn't read the source again")
            jid = bridge.vs_start(self.cfg, src, it["headline"], "long")
            self._set(iid, long_status="rendering", long_job=jid)
            self.log(f"Explainer: Video Studio job {jid} started for {it.get('job')}")
            while not self.stop.is_set():
                try: j = bridge.vs_job(self.cfg, jid)
                except bridge.BridgeError: self.stop.wait(30); continue
                st = j.get("status")
                self._set(iid, long_stage=j.get("stage"), long_prog=j.get("prog"))
                if st == "finished": break
                if st in ("error", "stopped"): raise RuntimeError(f"Video Studio job {jid} {st}: {j.get('error', '')[:300]}")
                self.stop.wait(20)
            else:
                return   # shutting down: resume() picks the watch up again
            self._attach_explainer(iid, jid)
        except Exception as e:
            self._set(iid, long_status="failed", long_error=str(e)[:400]); self.log(f"Explainer failed for {iid}: {e}")

    def _attach_explainer(self, iid, jid):
        it = self.item(iid)
        src = Path(self.cfg["video_studio_jobs"]) / jid
        dst = Path(it["staged"])
        moved = 0
        for f in (src / "output").iterdir() if (src / "output").is_dir() else []:
            n = f.name
            if f.is_file() and not n.startswith("_") and not n.endswith("_short.mp4") and "short" not in n and \
                    (("_explainer_" in n and f.suffix == ".mp4") or n in ("youtube_description.txt", "thumbnail_16_9.png",
                                                                       "thumbnail_16_9.jpg", "thumbnail.png", "chapters.txt")
                     or n.endswith(".srt") and "explainer" in n):
                shutil.copy2(f, dst / "output" / n); moved += "_explainer_" in n
        if not moved: raise RuntimeError(f"job {jid} finished but no explainer video was found")
        if (src / "plan.json").is_file(): shutil.copy2(src / "plan.json", dst / "plan.json")
        ai = sorted(set(ai_visuals(dst) + ai_visuals(src)))
        (dst / "ai_content.json").write_text(json.dumps({"altered": bool(ai), "why": ai}, indent=1), encoding="utf-8")
        self._set(iid, long_status="done", kinds=["long", "short"])
        self.log(f"Explainer attached to {it.get('job')} (from {jid}): publish does explainer first, then the Short with its link")

    # ------------------------------------------------------------ staged videos + publish
    def counts(self):
        """The queue at a glance: inbox items waiting (FIFO), being made, to review, published (24 h / all)."""
        items = self.state["items"].values()
        waiting = len([i for i in self.inbox_items(0, 10 ** 6)["items"] if i["status"] in ("new", "failed")])
        day = time.time() - 86400
        return {"waiting": waiting,
                "making": len([v for v in items if v.get("status") in BLOCKING or v.get("long_status") in ("researching", "rendering")]),
                "to_review": len([v for v in items if v.get("status") in UNREVIEWED]),
                "published_24h": len([v for v in items if v.get("status") == "published" and float(v.get("published_at") or 0) > day]),
                "published": len([v for v in items if v.get("status") == "published"]),
                "made_today": len([t for t in self.state.get("starts", []) if t > time.time() - 86400]),
                "paused_jobs": len([v for v in items if v.get("status") == "paused"]),
                "stalled": len([v for v in items if v.get("status") == "stalled"]),
                "cap": self.capped(),
                "laptop": {"enabled": bool((self.cfg.get("laptop") or {}).get("enabled")), **(self.state.get("laptop_seen") or {}),
                           "making": next((v.get("headline", "")[:60] for v in items if _on_laptop(v) and v.get("status") in ("researching", "rendering")), "")},
                "max_unreviewed": int(self.cfg.get("max_unreviewed", 5)), "max_per_day": int(self.cfg.get("max_videos_per_day", 10)),
                "paused": (Path(self.cfg["state_file"]).parent / "hold").exists()}

    def staged(self, include_rejected=False):
        keep = ("staged", "publishing", "published", "publish_failed") + (("rejected",) if include_rejected else ())
        out = [dict(v, id=k) for k, v in self.state["items"].items() if v.get("status") in keep
               and v.get("staged")]
        return sorted(out, key=lambda v: v.get("staged_at", 0), reverse=True)

    def video_path(self, iid, kind):
        it = self.item(iid) or {}
        out = Path(it.get("staged", "")) / "output"
        for f in sorted(out.glob("*_short.mp4" if kind == "short" else "*_explainer_*.mp4")):
            if not f.name.startswith("_"): return f
        return None

    def publish(self, iid, channel, visibility=None, kinds=None):
        it = self.item(iid)
        remaining = [k for k in (it or {}).get("kinds") or ["short"] if not ((it or {}).get("published") or {}).get(k)]
        if not it or not (it.get("status") in ("staged", "publish_failed") or (it.get("status") == "published" and remaining)):
            raise ValueError("nothing staged to publish for this item")
        if any(v.get("status") == "publishing" for v in self.state["items"].values()):
            raise ValueError("another video is being published: one at a time")
        if channel not in (self.cfg.get("channels") or []): raise ValueError(f"unknown channel {channel}")
        visibility = visibility or self.cfg.get("visibility", "Private")
        if visibility not in ("Private", "Unlisted", "Public"): raise ValueError("visibility must be Private, Unlisted or Public")
        kinds = [k for k in (kinds or remaining) if k in remaining]
        if not kinds: raise ValueError("that video is already published")
        kinds = ["long", "short"] if set(kinds) == {"long", "short"} else [kinds[0]]
        # a failed upload is finished on the same YouTube video (Retry), never uploaded a second time
        retry = it.get("status") == "publish_failed" and len(kinds) == 1 and kinds[0] in (it.get("failed_kinds") or kinds)
        r = bridge.pub_publish(self.cfg, it["job"], kinds, channel, visibility, mode="resume" if retry else "new")
        self._set(iid, status="publishing", channel=channel, visibility=visibility, tasks=r["tasks"], error="")
        self.log(f"Publishing {it['job']} ({' + '.join(kinds)}) to {channel} as {visibility}")
        self._spawn("pub_" + iid, self._watch_publish, iid)
        return self.item(iid)

    def resubmit(self, iid, channel, visibility=None, kinds=None, move=False):
        """Upload this video to ANOTHER channel (it keeps living where it already is). With move=True, once the
        upload succeeds the item waits as move_pending: nothing is deleted until move_delete is called — that call
        is Tariq's separate confirm (his rule: upload first, delete only after success, never automatic)."""
        it = self.item(iid)
        if not it or not it.get("staged"): raise ValueError("nothing staged for this item")
        if it.get("status") == "publishing" or it.get("resub"): raise ValueError("an upload is already running for this item")
        if it.get("move_pending"): raise ValueError("a move is already waiting: confirm or cancel its delete first")
        if any(v.get("status") == "publishing" for v in self.state["items"].values()):
            raise ValueError("another video is being published: one at a time")
        if channel not in (self.cfg.get("channels") or []): raise ValueError(f"unknown channel {channel}")
        visibility = visibility or self.cfg.get("visibility", "Private")
        if visibility not in ("Private", "Unlisted", "Public"): raise ValueError("visibility must be Private, Unlisted or Public")
        published = it.get("published") or {}
        all_kinds = it.get("kinds") or ["short"]
        revive = it.get("status") == "rejected"
        if revive: want = list(all_kinds)
        elif it.get("status") in ("published", "publish_failed"): want = [k for k in all_kinds if published.get(k)]
        else: raise ValueError("only a published or rejected video can be submitted again")
        if kinds: want = [k for k in kinds if k in want]
        if not want: raise ValueError("no published part of this video to submit again")
        have = (it.get("pub_channels") or {}).get(channel) or {}
        if all(have.get(k) for k in want) or (channel == it.get("channel") and all(published.get(k) for k in want)):
            raise ValueError(f"this video is already on {channel}")
        move_from = None
        if move:
            if revive: raise ValueError("a rejected video has no channel to move from")
            move_from = it.get("channel") or ""
            if not move_from or move_from == channel: raise ValueError("pick a different channel to move to")
        want = ["long", "short"] if set(want) == {"long", "short"} else want
        r = bridge.pub_publish(self.cfg, it["job"], want, channel, visibility, mode="new")
        self._set(iid, status="publishing", tasks=r["tasks"], error="",
                  resub={"channel": channel, "visibility": visibility, "kinds": want, "move_from": move_from,
                         "revive": revive, "prev_status": it.get("status"), "at": time.time()})
        self.log(f"{'Moving' if move else 'Submitting again'} {it['job']} ({' + '.join(want)}) to {channel} as {visibility}"
                 + (f" (from {move_from}; the delete waits for a separate confirm)" if move else ""))
        self._spawn("pub_" + iid, self._watch_resubmit, iid)
        return self.item(iid)

    def move_delete(self, iid):
        """Second half of a move: the upload to the new channel already succeeded (move_pending). Delete the video
        from the old channel through the Publisher, then make the new channel the item's home. Called ONLY by
        Tariq's explicit confirm — never automatically."""
        it = self.item(iid); mp = (it or {}).get("move_pending")
        if not it or not mp: raise ValueError("no move is waiting on a delete for this item")
        frm, to = mp.get("from") or "", mp.get("to") or ""
        pcs = {**(it.get("pub_channels") or {})}
        src = pcs.get(frm) or {}
        kinds = [k for k in (mp.get("kinds") or list(src)) if k in src] or list(src)
        if not kinds:   # legacy item (published before pub_channels existed): fall back to the primary map
            kinds = [k for k, u in (it.get("published") or {}).items() if u]
            src = {k: {"url": (it.get("published") or {}).get(k, ""), "video_id": _video_id((it.get("published") or {}).get(k, ""))} for k in kinds}
        if not kinds: raise ValueError("nothing on the old channel to delete")
        for k in kinds:
            entry = src.get(k) or {}
            bridge.pub_delete(self.cfg, frm, it.get("job") or "", k, entry.get("video_id") or _video_id(entry.get("url", "")))
        pcs.pop(frm, None)
        kw = {"move_pending": None, "pub_channels": pcs, "error": ""}
        if (it.get("channel") or "") == frm and to:
            dest = pcs.get(to) or {}
            newpub = {k: (dest.get(k) or {}).get("url") or u for k, u in (mp.get("urls") or {}).items()}
            kw.update(channel=to, published={**(it.get("published") or {}), **{k: v for k, v in newpub.items() if v}})
        self._set(iid, **kw)
        self.log(f"Moved {it.get('job')}: deleted from {frm}; home channel is now {to}")
        return self.item(iid)

    def move_cancel(self, iid):
        """Give up on a waiting move: the video simply stays on both channels (a plain resubmit)."""
        it = self.item(iid)
        if not it or not it.get("move_pending"): raise ValueError("no move is waiting for this item")
        self._set(iid, move_pending=None)
        self.log(f"Move cancelled for {it.get('job')}: it stays on both channels")
        return self.item(iid)

    def _pub_entry(self, it, channel, new_urls):
        """Merge freshly published urls into the item's per-channel record (pub_channels)."""
        pcs = {**(it.get("pub_channels") or {})}
        if channel:
            cur = {**(pcs.get(channel) or {})}
            for k, u in (new_urls or {}).items():
                if u: cur[k] = {"url": u, "video_id": _video_id(u), "at": time.time()}
            if cur: pcs[channel] = cur
        return pcs

    def _watch_resubmit(self, iid):
        it = self.item(iid); urls = {}
        while not self.stop.is_set():
            states = {}
            for kind, tid in it["tasks"].items():
                try: states[kind] = bridge.pub_task(self.cfg, tid)
                except bridge.BridgeError as e: states[kind] = {"status": "unknown", "error": str(e)}
            self._set(iid, publish_progress={k: {"status": v.get("status"), "last": (v.get("events") or [{}])[-1] if v.get("events") else {},
                                                 "error": v.get("error", "")} for k, v in states.items()})
            for k, v in states.items():
                if v.get("status") == "complete": urls[k] = (v.get("result") or {}).get("url", "")
            rs = it.get("resub") or {}
            failed = [k for k, v in states.items() if v.get("status") in ("error", "cancelled")]
            if failed:
                pcs = self._pub_entry(it, rs.get("channel") or "", urls)   # whatever finished IS live on the new channel
                kw = {"resub": None, "pub_channels": pcs,
                      "error": ("submit again: " + "; ".join(f"{k}: {states[k].get('error', states[k].get('status'))}" for k in failed))[:400]}
                if rs.get("revive"):
                    if urls: kw.update(status="published", published={**(it.get("published") or {}), **urls},
                                       channel=rs.get("channel"), published_at=time.time())
                    else: kw.update(status="rejected")
                else:
                    kw.update(status=rs.get("prev_status") or "published")
                self._set(iid, **kw)
                self.log(f"Submit again failed for {it['job']}: {kw['error']}")
                threading.Thread(target=self.pump_publish, daemon=True).start()
                return
            if len(urls) == len(it["tasks"]):
                pcs = self._pub_entry(it, rs.get("channel") or "", urls)
                kw = {"resub": None, "pub_channels": pcs, "error": "", "status": "published"}
                if rs.get("revive"):
                    kw.update(published={**(it.get("published") or {}), **urls}, channel=rs.get("channel"), published_at=time.time())
                elif rs.get("move_from"):
                    kw.update(move_pending={"from": rs["move_from"], "to": rs.get("channel"),
                                            "kinds": rs.get("kinds") or list(urls), "urls": urls, "at": time.time()})
                self._set(iid, **kw)
                self.log(f"{'Move upload done' if rs.get('move_from') else 'Submitted again'} {it['job']} to {rs.get('channel')}: {urls}"
                         + (" (delete from the old channel waits for Tariq's confirm)" if rs.get("move_from") else ""))
                threading.Thread(target=self.pump_publish, daemon=True).start()
                return
            self.stop.wait(10)

    # ------------------------------------------------------------ approve several, publish them one by one
    def approve(self, iid, on=True, channel=None, visibility=None, kinds=None):
        """Tick a staged video for the next "Publish approved" (its channel / visibility / kinds as chosen on its card)."""
        it = self.item(iid)
        if not it or not it.get("staged"): raise ValueError("nothing staged for this item")
        if not on:
            self._set(iid, approved=None); return self.item(iid)
        channel = (channel or it.get("channel") or (self.cfg.get("channels") or [""])[0]).lower()
        if channel not in (self.cfg.get("channels") or []): raise ValueError(f"unknown channel {channel}")
        visibility = visibility or self.cfg.get("visibility", "Private")
        if visibility not in ("Private", "Unlisted", "Public"): raise ValueError("visibility must be Private, Unlisted or Public")
        self._set(iid, approved={"channel": channel, "visibility": visibility, "kinds": kinds or None, "at": time.time()})
        return self.item(iid)

    def publish_approved(self):
        """Queue every ticked video (oldest tick first); the background loop publishes them one at a time."""
        q = self.state.setdefault("publish_queue", [])
        ticked = sorted(((k, v) for k, v in self.state["items"].items() if v.get("approved") and k not in q),
                        key=lambda kv: kv[1]["approved"].get("at", 0))
        q += [k for k, _ in ticked]; self.save()
        if ticked: self.log(f"Publish approved: {len(ticked)} video(s) queued ({len(q)} waiting)")
        self.pump_publish()
        return q

    def pump_publish(self):
        """Traffic cop: when nothing is uploading, publish the next queued video."""
        with _start_lock:
            q = self.state.get("publish_queue") or []
            if not q or any(v.get("status") == "publishing" for v in self.state["items"].values()): return None
            iid = q.pop(0); self.save()
            it = self.item(iid) or {}; a = it.get("approved") or {}
            try:
                self.publish(iid, a.get("channel") or "", a.get("visibility"), a.get("kinds"))
                self._set(iid, approved=None)
                return iid
            except (ValueError, bridge.BridgeError) as e:
                self.log(f"Publish approved: skipped {it.get('job')} ({e})")
                self._set(iid, approved=None)
        return self.pump_publish()

    def reject(self, iid, reason=""):
        """You don't want this video: it leaves the Staging panel and the queue moves on to the next item."""
        it = self.item(iid)
        if not it or it.get("status") not in ("staged", "publish_failed", "withheld"): raise ValueError("nothing staged to reject for this item")
        self._set(iid, status="rejected", error=(reason or "rejected in the Newsroom")[:300])
        self.log(f"Rejected {it.get('job')}: {it.get('headline', '')[:80]}" + (f" ({reason[:120]})" if reason else ""))
        if reason.strip():   # the reason teaches the house style too (in the background: the page doesn't wait for the AI)
            threading.Thread(target=self._feedback_quiet, args=(iid, "Rejected: " + reason), daemon=True).start()
        self.queue_line(inbox.rejected_line(it.get("headline", ""), it.get("job") or "-"))
        return self.item(iid)

    def feedback(self, iid, note):
        """Your note on a video -> standing house-style rules in Video Studio (every later script and visual follows them)."""
        it = self.item(iid)
        if not it: raise ValueError("unknown item")
        note = " ".join(str(note).split())
        if not note: raise ValueError("write a note first")
        ctx = f"{'Short and explainer' if it.get('mode') == 'both' else (it.get('mode') or 'Short')} about '{it.get('vs_title') or it.get('headline', '')}'"
        r = bridge.vs_style_learn(self.cfg, note, ctx, it.get("job") or "")
        with _lock:
            fb = list(it.get("feedback") or []) + [{"at": time.time(), "note": note[:1000], "rules": [x["rule"] for x in r.get("added", [])]}]
        self._set(iid, feedback=fb)
        self.log(f"Feedback on {it.get('job')}: {len(r.get('added', []))} house-style rule(s) learned" +
                 "".join(f" | {x['rule'][:90]}" for x in r.get("added", [])))
        return r

    def _feedback_quiet(self, iid, note):
        try: self.feedback(iid, note)
        except Exception as e: self.log(f"Feedback not learned ({str(e)[:150]})")

    def _watch_publish(self, iid):
        it = self.item(iid); urls = {}
        while not self.stop.is_set():
            states = {}
            for kind, tid in it["tasks"].items():
                try: states[kind] = bridge.pub_task(self.cfg, tid)
                except bridge.BridgeError as e: states[kind] = {"status": "unknown", "error": str(e)}
            self._set(iid, publish_progress={k: {"status": v.get("status"), "last": (v.get("events") or [{}])[-1] if v.get("events") else {},
                                                 "error": v.get("error", "")} for k, v in states.items()})
            for k, v in states.items():
                if v.get("status") == "complete": urls[k] = (v.get("result") or {}).get("url", "")
            failed = [k for k, v in states.items() if v.get("status") in ("error", "cancelled")]
            if failed:
                self._set(iid, status="publish_failed", published={**(it.get("published") or {}), **urls}, failed_kinds=failed,
                          pub_channels=self._pub_entry(it, it.get("channel") or "", urls),
                          error="; ".join(f"{k}: {states[k].get('error', states[k].get('status'))}" for k in failed)[:400])
                self.log(f"Publish failed for {it['job']}: {self.item(iid)['error']}")
                threading.Thread(target=self.pump_publish, daemon=True).start()   # the queue moves on; this one stays for a retry
                return
            if len(urls) == len(it["tasks"]):
                pcs = self._pub_entry(it, it.get("channel") or "", urls)
                urls = {**(it.get("published") or {}), **urls}   # an explainer published earlier keeps its link
                self._set(iid, status="published", published=urls, published_at=time.time(), pub_channels=pcs)
                link = urls.get("long") or urls.get("short") or next(iter(urls.values()), "")
                self.log(f"Published {it['job']}: {urls}")
                self.queue_line(inbox.published_line(it["headline"], link))
                threading.Thread(target=self.pump_publish, daemon=True).start()
                return
            self.stop.wait(10)

    # ------------------------------------------------------------ start-up
    def resume(self):
        for iid, it in list(self.state["items"].items()):
            if it.get("status") in ("rendering", "paused") and it.get("job"):
                self.log(f"Resuming the watch on {it['job']}"); self._spawn(iid, self._watch, iid)
            elif it.get("status") == "researching":
                self._set(iid, status="failed", error="the Newsroom restarted during research: press Generate again")
            elif it.get("status") == "publishing" and it.get("tasks"):
                self._spawn("pub_" + iid, self._watch_resubmit if it.get("resub") else self._watch_publish, iid)
            if it.get("long_status") == "rendering" and it.get("long_job"):
                self._spawn("long_" + iid, self._resume_explainer, iid)
            elif it.get("long_status") == "researching":
                self._set(iid, long_status="queued")

    def _resume_explainer(self, iid):
        it = self.item(iid); jid = it["long_job"]
        self.log(f"Resuming the explainer watch on {jid}")
        try:
            while not self.stop.is_set():
                try: j = bridge.vs_job(self.cfg, jid)
                except bridge.BridgeError: self.stop.wait(30); continue
                if j.get("status") == "finished": return self._attach_explainer(iid, jid)
                if j.get("status") in ("error", "stopped"): raise RuntimeError(f"Video Studio job {jid} {j.get('status')}")
                self._set(iid, long_stage=j.get("stage"), long_prog=j.get("prog")); self.stop.wait(20)
        except Exception as e:
            self._set(iid, long_status="failed", long_error=str(e)[:400]); self.log(f"Explainer failed for {iid}: {e}")

    def auto_next(self):
        """Autonomous mode, one video at a time, when the pipeline is idle: 1) a [BREAKING] item, 2) a paused video resumes
        (exempt from the caps), 3) the oldest new item (FIFO). New items obey capped(). -> item id or None.
        A failed item is retried automatically up to auto_retries times, except when its source couldn't be read."""
        if not self.cfg.get("auto_generate", True) or self.held(): return None
        busy = [(k, v) for k, v in self.state["items"].items() if (v.get("status") in BLOCKING and not _on_laptop(v))
                or v.get("long_status") in ("queued", "researching", "rendering")]
        if busy:
            self._breaking_preempt(busy)
            return None
        try:
            if bridge.vs_status(self.cfg).get("current"): return None   # Video Studio busy (any job): wait
        except bridge.BridgeError: return None   # Video Studio is down / restarting: try again next minute
        cap = self.capped()
        bcap = self.capped(breaking=True)
        items = list(reversed(self.inbox_items(0, 10 ** 6)["items"]))   # oldest first
        def startable(it):
            loc = self.item(it["id"]) or {}
            st = loc.get("status", "new")
            return st == "new" or (st == "failed" and loc.get("attempts", 1) <= int(self.cfg.get("auto_retries", 1))
                                   and not (str(loc.get("error", "")).startswith("couldn't read") and "no other outlet" in str(loc.get("error", ""))))
        def start(it):
            loc = self.item(it["id"]) or {}
            self.log(f"Auto: {'BREAKING ' if it.get('breaking') else ''}inbox item -> Generate: {it['headline'][:80]}")
            try:
                self.generate(it["id"]); self._started()
                self._set(it["id"], attempts=loc.get("attempts", 0) + 1, auto=True)
                return it["id"]
            except ValueError as e:
                self.log(f"Auto: skipped ({e})")
        if not bcap:
            for it in items:
                if it.get("breaking") and startable(it) and start(it): return it["id"]
        paused = sorted(((k, v) for k, v in self.state["items"].items() if not _on_laptop(v) and
                         (v.get("status") == "paused" or (v.get("status") == "stalled" and v.get("resume_req")))),
                        key=lambda kv: kv[1].get("paused_at", 0))
        for k, _v in paused:
            try: self.resume_job(k, auto=True); return k
            except (ValueError, bridge.BridgeError) as e: self.log(f"Auto: resume skipped ({e})"); return None
        if cap: return None
        for it in items:
            if startable(it) and start(it): return it["id"]
        return None

    def offload_next(self):
        """Second worker: whenever a story is waiting and the laptop is on, plugged in and idle, it makes the oldest waiting one
        (whole video, its own pace; the PC takes the next). laptop.backlog = how many must wait first (1 = any).
        Laptop jobs don't count toward the PC's hourly / daily limits. Done = staged ([PROCESSED]), not published."""
        lc = self.cfg.get("laptop") or {}
        if not lc.get("enabled") or self.held(): return None
        if any(_on_laptop(v) and v.get("status") in ("researching", "rendering") for v in self.state["items"].values()): return None
        held = sorted(((k, v) for k, v in self.state["items"].items() if _on_laptop(v) and
                       (v.get("status") == "paused" or (v.get("status") == "stalled" and v.get("resume_req")))),
                      key=lambda kv: kv[1].get("paused_at", 0))
        for k, _v in held:   # a paused / resume-queued laptop video goes before any new story
            try: self.resume_job(k, auto=True); return k
            except (ValueError, bridge.BridgeError) as e: self.log(f"Laptop: resume skipped ({e})"); return None
        items = list(reversed(self.inbox_items(0, 10 ** 6)["items"]))   # oldest first
        waiting = [it for it in items if (self.item(it["id"]) or {}).get("status", "new") == "new" and not it.get("breaking")]
        if len(waiting) < int(lc.get("backlog", 1) or 1): return None
        base, st = laptop.find(self.cfg, self.state)
        self.state["laptop_seen"] = {"at": time.time(), "up": bool(base), "ready": laptop.ready(st), "ac": ((st or {}).get("power") or {}).get("ac")}
        if not base or not laptop.ready(st): return None
        it = waiting[0]
        with _start_lock:
            if (self.item(it["id"]) or {}).get("status", "new") != "new": return None
            self._set(it["id"], headline=it["headline"], url=it["url"], pitch=it["pitch"], brief=it.get("brief", ""), breaking=False,
                      status="researching", mode="short", want_long=self.cfg.get("mode", "short") in ("both", "long"),
                      error="", job=None, created=time.time(), worker="laptop", worker_url=base)
        self.log(f"Laptop: {len(waiting)} waiting -> {it['headline'][:70]}")
        self._spawn(it["id"], self._run_laptop, it["id"])
        return it["id"]

    LAPTOP_SETTINGS = {"ladder_v2": True}   # no WanGP on the laptop: visuals drop to the next rung instead of stopping

    def _laptop_prep(self, it):
        """Keep the laptop's Video Studio settings in line with what laptop jobs need (laptop.settings adds more)."""
        want = {**self.LAPTOP_SETTINGS, **((self.cfg.get("laptop") or {}).get("settings") or {})}
        try: bridge.vs_settings(self._vcfg(it), want)
        except bridge.BridgeError as e: self.log(f"  laptop settings not updated ({str(e)[:120]})")

    def _run_laptop(self, iid):
        it = self.item(iid)
        self._laptop_prep(it)
        src = self._research(iid)
        if not src: return
        try: bridge.vs_style_import(self._vcfg(it), bridge.vs_style(self.cfg))   # the laptop writes in the same house style
        except bridge.BridgeError as e: self.log(f"  house style not copied to the laptop ({str(e)[:120]})")
        jid = bridge.vs_start(self._vcfg(it), src, it["headline"], it.get("mode"))
        self._set(iid, status="rendering", job=jid, source_kind=src["kind"], source_words=src.get("words", 0))
        self.log(f"Laptop job {jid} started for: {it['headline'][:80]}")
        self._watch(iid)

    def background(self):
        """Inbox refresh every inbox_refresh_minutes; autonomous Generate for new items; queued doc lines retried every minute."""
        def loop():
            last = 0
            while not self.stop.is_set():
                if time.time() - last > float(self.cfg.get("inbox_refresh_minutes", 5)) * 60:
                    self.refresh_inbox(); last = time.time()
                try: self.pump_publish()
                except Exception as e: self.log(f"Publish queue: error ({type(e).__name__}: {e})")
                try: self.offload_next()
                except Exception as e: self.log(f"Laptop: error ({type(e).__name__}: {e})")
                try: self.explainer_next() or self.auto_next()
                except Exception as e: self.log(f"Auto: error ({type(e).__name__}: {e})")
                if self.state["pending_writes"]: self.flush_writes()
                self.stop.wait(60)
        threading.Thread(target=loop, daemon=True).start()
