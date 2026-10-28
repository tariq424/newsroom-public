"""Mock-data tests: a fake Video Studio, a fake Publisher and a fake news site run on local ports; the inbox is a local file
with the Newsroom Inbox line format. Covers inbox parsing, research, Generate -> stage -> [PROCESSED], Publish ->
[PUBLISHED], the HTTP endpoints and the GUI page."""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from flask import Flask, jsonify, request
from werkzeug.serving import make_server

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from newsroom import bridge, core, inbox, research  # noqa: E402

ARTICLE = "<html><head><title>x</title><meta property='og:site_name' content='Tech Daily'></head><body><nav><p>Home News Sports Weather Login Subscribe today now</p></nav><article>" + "".join(
    f"<p>Paragraph {i}: the new open model from the lab beats last year's best results on coding benchmarks while costing far less to run.</p>"
    for i in range(12)) + "</article><footer><p>All rights reserved by the publisher of this site forever.</p></footer></body></html>"

INBOX = """[PENDING] Old story about chips | https://news.example/old | Older pitch.
[PENDING] Lab ships open model that beats GPT on code | https://www.google.com/url?rct=j&sa=t&url={base}/article&ct=ga | A new open model tops coding benchmarks. Cheap to run.
[PENDING] Already done story | https://news.example/done | Done.
[PROCESSED] Already done story - 20261020_000000
"""


def serve(app):
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


class Fakes:
    def __init__(self, jobs_dir):
        self.jobs_dir = Path(jobs_dir); self.posted = []; self.published = []; self.polls = 0
        vs = Flask("vs"); pub = Flask("pub"); site = Flask("site")

        @site.get("/article")
        def article(): return ARTICLE

        @vs.get("/api/status")
        def vs_status(): return jsonify(current=None, wangp=False, worker="fake", power={"ac": True})

        @vs.get("/api/jobs/<jid>/outputs")
        def vs_outputs(jid):
            d = self.jobs_dir / jid
            return jsonify(files=[str(f.relative_to(d)).replace("\\", "/") for f in d.rglob("*") if f.is_file() and not f.name.startswith("_")])

        @vs.post("/api/jobs")
        def vs_jobs():
            self.posted.append(request.get_json()); jid = f"20261021_12000{len(self.posted) - 1}"
            self.modes = getattr(self, "modes", {}); self.modes[jid] = self.posted[-1]["mode"]
            d = self.jobs_dir / jid; (d / "output").mkdir(parents=True); (d / "short").mkdir()
            (d / "short" / "plan.json").write_text(json.dumps({"title": "Open model beats GPT", "sections": [{"id": "b0", "vo": "A lab shipped an open model."}]}))
            return jsonify(id=jid)

        @vs.get("/api/jobs/<jid>")
        def vs_job(jid):
            self.polls += 1
            if self.polls >= 2:   # finishes on the second poll
                d = self.jobs_dir / jid / "output"
                if self.modes.get(jid) == "long":
                    (d / "Open_model_explainer_15min.mp4").write_bytes(b"\x00" * 4096); (d / "youtube_description.txt").write_text("long desc")
                    (d / "thumbnail_16_9.png").write_bytes(b"png"); (self.jobs_dir / jid / "plan.json").write_text('{"title": "Long"}')
                    return jsonify(status="finished", stage="render", title="Open model explained", log=["done"], stages=["a"], done=["a"])
                (d / "Open_model_short.mp4").write_bytes(b"\x00" * 2048); (d / "youtube_short_description.txt").write_text("desc")
                (d / "_debug.jpg").write_bytes(b"x")
                pub = self.jobs_dir / jid / "short" / "public"; pub.mkdir(parents=True, exist_ok=True)
                (pub / "teaser.json").write_text(json.dumps({"beats": [{"bgs": [{"tag": "GENERATED · GEMINI"}, {"type": "video"}]}]}))
                return jsonify(status="finished", stage="short_render", title="Open model beats GPT", log=["done"], stages=["a"], done=["a"])
            return jsonify(status="running", stage="short_script", title="", log=["working"], stages=["a", "b"], done=["a"])

        @vs.get("/files/<jid>/<path:p>")
        def vs_files(jid, p):
            f = self.jobs_dir / jid / p
            return (f.read_text(), 200) if f.exists() else ("", 404)

        @pub.get("/health")
        def health(): return jsonify(service="youtube-publisher")

        @pub.get("/api/defaults/<job>/<kind>/<channel>")
        def defaults(job, kind, channel): return jsonify(data={"job": job, "kind": kind, "channel": channel, "title": "T", "visibility": "Private"})

        @pub.post("/publish")
        def publish(): self.published.append(request.get_json()); return jsonify(task_id="t1"), 202

        @pub.get("/publish/status/<tid>")
        def pstatus(tid): return jsonify(status="complete", events=[{"message": "done"}], result={"url": "https://youtu.be/abc123"})

        self.servers = [serve(a) for a in (vs, pub, site)]
        self.vs, self.pub, self.site = [u for _, u in self.servers]

    def close(self):
        for s, _ in self.servers: s.shutdown()


class InboxFormat(unittest.TestCase):
    def test_parse_filter_unwrap(self):
        items = inbox.active(INBOX.format(base="https://news.example"))
        self.assertEqual([i["headline"] for i in items], ["Lab ships open model that beats GPT on code", "Old story about chips"])   # newest first
        self.assertEqual(items[0]["url"], "https://news.example/article")   # Google Alerts redirect unwrapped
        self.assertEqual(items[0]["pitch"], "A new open model tops coding benchmarks. Cheap to run.")

    def test_published_also_filters(self):
        t = "[PENDING] A | https://x.y/a | p\n[PUBLISHED] A - https://youtu.be/x\n"
        self.assertEqual(inbox.active(t), [])

    def test_lines(self):
        self.assertEqual(inbox.processed_line("Big | news", "J1"), "[PROCESSED] Big / news - J1")
        self.assertEqual(inbox.published_line("Big news", "https://youtu.be/x"), "[PUBLISHED] Big news - https://youtu.be/x")


class DocQuotes(unittest.TestCase):
    def test_curly_quotes_from_google_docs_still_match(self):
        from newsroom import gdoc
        self.assertTrue(gdoc.has_line("x\n[PROCESSED] \u2018De-CATLization\u2019 tests China\u2019s battery king - J1\n",
                                      "[PROCESSED] 'De-CATLization' tests China's battery king - J1"))


class Research(unittest.TestCase):
    def test_article_text_without_nav_and_footer(self):
        a = research.extract(ARTICLE)
        self.assertEqual(a["site"], "Tech Daily")
        self.assertGreater(a["words"], 120)
        self.assertNotIn("Subscribe", a["text"]); self.assertNotIn("rights reserved", a["text"])

    def test_youtube_is_passed_through(self):
        self.assertEqual(research.fetch("https://youtu.be/g3toZwkrQQE")["kind"], "youtube")


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()); self.fakes = Fakes(self.tmp / "vsjobs")
        self.inbox_file = self.tmp / "inbox.txt"
        self.inbox_file.write_text(INBOX.format(base=self.fakes.site), encoding="utf-8")
        self.cfg = {"video_studio": self.fakes.vs, "publisher": self.fakes.pub, "video_studio_jobs": str(self.tmp / "vsjobs"),
                    "staging_dir": str(self.tmp / "staging"), "state_file": str(self.tmp / "state.json"), "inbox_source": "file",
                    "inbox_file": str(self.inbox_file), "mode": "short", "audience": "beginner", "channels": ["yousocial"],
                    "visibility": "Private", "chrome_profile": str(self.tmp / "noprofile"), "minutes": 8, "short_secs": 165, "min_minutes_between_videos": 0}
        core.Newsroom._watch_interval = 0

    def tearDown(self):
        self.fakes.close()

    def wait(self, room, iid, statuses, secs=60):
        t0 = time.time()
        while time.time() - t0 < secs:
            if (room.item(iid) or {}).get("status") in statuses: return room.item(iid)
            time.sleep(0.3)
        self.fail(f"status stayed {(room.item(iid) or {}).get('status')}: {(room.item(iid) or {}).get('error')}")

    def test_generate_stage_publish(self):
        room = core.Newsroom(self.cfg)
        room.refresh_inbox()
        first = room.inbox_items()["items"][0]
        self.assertEqual(first["headline"], "Lab ships open model that beats GPT on code")
        room.generate(first["id"])
        it = self.wait(room, first["id"], ("staged", "failed"), 90)
        self.assertEqual(it["status"], "staged", it.get("error"))
        body = self.fakes.posted[0]
        self.assertEqual((body["url"], body["mode"], body["audience"], body["auto_approve"], body["src_credit"]), ("", "short", "beginner", True, "Tech Daily"))
        self.assertIn("coding benchmarks", body["transcript"])
        staged = Path(it["staged"])
        self.assertTrue((staged / "output" / "Open_model_short.mp4").is_file())
        self.assertFalse((staged / "output" / "_debug.jpg").exists())
        self.assertTrue((staged / "short" / "plan.json").is_file())
        text = self.inbox_file.read_text()
        self.assertIn("[PROCESSED] Lab ships open model that beats GPT on code - 20261021_120000", text)
        room.refresh_inbox()
        self.assertNotIn(first["id"], [i["id"] for i in room.inbox_items()["items"]])   # processed: gone from the inbox
        self.assertEqual(bridge.vs_script(self.cfg, it["job"])["beats"][0]["vo"], "A lab shipped an open model.")
        room.publish(first["id"], "yousocial")
        it = self.wait(room, first["id"], ("published", "publish_failed"))
        self.assertEqual(it["status"], "published")
        self.assertEqual(self.fakes.published[0]["visibility"], "Private")
        self.assertIn("[PUBLISHED] Lab ships open model that beats GPT on code - https://youtu.be/abc123", self.inbox_file.read_text())

    def test_autonomous_generate_picks_the_oldest_new_item_and_marks_ai_content(self):
        room = core.Newsroom(self.cfg); room.refresh_inbox()
        # put the newest item's [PROCESSED] aside: the oldest pending one must go first
        first = room.auto_next()
        self.assertEqual(room.item(first)["headline"], "Old story about chips")
        self.assertIsNone(room.auto_next())   # one at a time
        it = self.wait(room, first, ("staged", "failed"), 90)
        self.assertEqual(it["status"], "failed")   # news.example isn't reachable in the test: research fails...
        self.assertIn("couldn't read", it["error"])
        nxt = room.auto_next()                    # ...is not retried, and the next item starts
        self.assertEqual(room.item(nxt)["headline"], "Lab ships open model that beats GPT on code")
        it = self.wait(room, nxt, ("staged", "failed"), 90)
        self.assertEqual(it["status"], "staged", it.get("error"))
        self.assertEqual(json.loads((Path(it["staged"]) / "ai_content.json").read_text()), {"altered": True, "why": ["GENERATED · GEMINI"]})
        # a video waiting in Staging never stops production: the next one starts
        self.inbox_file.write_text(self.inbox_file.read_text() + f"[PENDING] Third story | {self.fakes.site}/article | p\n")
        room.refresh_inbox()
        self.assertEqual(room.item(room.auto_next())["headline"], "Third story")
        room.reject(nxt)
        self.assertEqual(room.item(nxt)["status"], "rejected")

    def test_laptop_takes_the_oldest_story_when_the_queue_is_backed_up(self):
        port = self.fakes.vs.rsplit(":", 1)[1]
        cfg = dict(self.cfg, video_studio="http://127.0.0.1:1", laptop={"enabled": True, "name": "127.0.0.1", "port": int(port), "backlog": 2},
                   laptop_token="t", video_studio_jobs=str(self.tmp / "nowhere"))   # the PC's own Video Studio is down here
        room = core.Newsroom(cfg); room.refresh_inbox()
        self.assertIsNone(core.Newsroom(dict(cfg, laptop={**cfg["laptop"], "backlog": 5})).offload_next())   # not backed up enough
        iid = room.offload_next()
        self.assertEqual(room.item(iid)["headline"], "Old story about chips")   # oldest first
        self.assertIsNone(room.offload_next())   # one job at a time on the laptop
        t0 = time.time()
        while room.item(iid)["status"] not in ("staged", "failed") and time.time() - t0 < 60: time.sleep(0.3)
        it = room.item(iid)
        if it["status"] == "failed":   # the fake news site isn't reachable for this story: try the next one
            self.assertIn("couldn't read", it["error"])
            room._set(iid, status="rejected"); room.cfg["laptop"]["backlog"] = 1
            iid = room.offload_next(); t0 = time.time()
            while room.item(iid)["status"] not in ("staged", "failed") and time.time() - t0 < 60: time.sleep(0.3)
            it = room.item(iid)
        self.assertEqual((it["status"], it["worker"]), ("staged", "laptop"), it.get("error"))
        self.assertTrue((Path(it["staged"]) / "output" / "Open_model_short.mp4").is_file())   # copied back from the "laptop"
        self.assertNotIn("starts", room.state)   # laptop jobs don't count toward the PC's limits

    def test_explainer_added_to_a_staged_short(self):
        room = core.Newsroom(self.cfg); room.refresh_inbox()
        first = next(i for i in room.inbox_items()["items"] if i["headline"].startswith("Lab ships"))["id"]
        room.generate(first)
        it = self.wait(room, first, ("staged", "failed"), 90)
        self.assertEqual(it["kinds"], ["short"])
        room.add_explainer(first)
        self.assertIsNone(room.auto_next())   # nothing new starts while an explainer is queued
        self.assertEqual(room.explainer_next(), first)
        t0 = time.time()
        while room.item(first).get("long_status") not in ("done", "failed") and time.time() - t0 < 90: time.sleep(0.3)
        it = room.item(first)
        self.assertEqual(it["long_status"], "done", it.get("long_error"))
        self.assertEqual(it["kinds"], ["long", "short"])
        out = Path(it["staged"]) / "output"
        self.assertTrue((out / "Open_model_explainer_15min.mp4").is_file() and (out / "Open_model_short.mp4").is_file())
        self.assertTrue((out / "youtube_description.txt").is_file() and (Path(it["staged"]) / "plan.json").is_file())
        self.assertEqual(self.fakes.posted[-1]["mode"], "long")
        self.assertEqual(room.video_path(first, "long").name, "Open_model_explainer_15min.mp4")

    def test_unreadable_article_does_not_start_a_job(self):
        self.inbox_file.write_text(f"[PENDING] Paywalled | {self.fakes.site}/missing | p\n")
        room = core.Newsroom(self.cfg); room.refresh_inbox()
        iid = room.inbox_items()["items"][0]["id"]
        room.generate(iid)
        it = self.wait(room, iid, ("failed",))
        self.assertIn("couldn't read enough article text", it["error"])
        self.assertEqual(self.fakes.posted, [])

    def test_doc_write_failure_is_queued_and_retried(self):
        room = core.Newsroom({**self.cfg, "inbox_source": "doc", "doc_id": ""})
        room.queue_line("[PROCESSED] X - J")
        self.assertEqual(room.state["pending_writes"], ["[PROCESSED] X - J"])
        room.cfg = self.cfg
        self.assertTrue(room.flush_writes())
        self.assertIn("[PROCESSED] X - J", self.inbox_file.read_text())

    def test_ignored_keywords_are_hidden(self):
        room = core.Newsroom({**self.cfg, "ignore_keywords": ["CHIPS"]}); room.refresh_inbox()
        self.assertEqual([i["headline"] for i in room.inbox_items()["items"]], ["Lab ships open model that beats GPT on code"])

    def test_concurrent_flushes_write_each_line_once(self):
        from unittest.mock import patch
        room = core.Newsroom(self.cfg)
        real = core.gdoc.append
        def slow(cfg, line): time.sleep(0.5); real(cfg, line)
        with patch.object(core.gdoc, "append", side_effect=slow):
            with core._lock: room.state["pending_writes"].append("[PROCESSED] Once - J1")
            ts = [threading.Thread(target=room.flush_writes) for _ in range(3)]
            [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(self.inbox_file.read_text().count("[PROCESSED] Once - J1"), 1)

    def test_http_endpoints_and_page(self):
        import importlib, os
        os.environ["NEWSROOM_NO_BACKGROUND"] = "1"
        app_mod = importlib.import_module("app")
        app_mod.cfg.clear(); app_mod.cfg.update(self.cfg)
        app_mod.room = core.Newsroom(app_mod.cfg)
        c = app_mod.app.test_client()
        self.assertIn(b"THE <span>NEWSROOM</span>", c.get("/", base_url="http://127.0.0.1:42040").data)
        r = c.post("/api/inbox/refresh", json={}, base_url="http://127.0.0.1:42040").get_json()
        self.assertEqual(len(r["items"]), 2)
        s = c.get("/api/status", base_url="http://127.0.0.1:42040").get_json()
        self.assertTrue(s["video_studio"]["ok"]); self.assertTrue(s["publisher"]["ok"])
        # the phone page from another device: only with the passcode, and only the review endpoints
        lan = dict(base_url="http://192.168.1.20:42040", environ_base={"REMOTE_ADDR": "192.168.1.30"})
        self.assertEqual(c.get("/m", **lan).status_code, 403)   # no passcode configured
        app_mod.cfg["mobile_pin"] = "4711"
        self.assertEqual(c.get("/m", **lan).status_code, 403)
        self.assertEqual(c.get("/m?k=4711", **lan).status_code, 200)   # sets the cookie
        self.assertEqual(c.get("/api/staged", **lan).status_code, 200)
        self.assertEqual(c.post("/api/generate", json={"id": "x"}, **lan).status_code, 403)   # not a phone action
        app_mod.cfg.pop("mobile_pin")
        bad = c.post("/api/generate", json={"id": "x"}, base_url="http://127.0.0.1:42040", headers={"Origin": "https://evil.example"})
        self.assertEqual(bad.status_code, 403)
        self.assertEqual(c.get("/", base_url="http://192.168.1.5:42040").status_code, 403)
        r = c.post("/api/alerts", json={"topic": ""}, base_url="http://127.0.0.1:42040")
        self.assertEqual(r.status_code, 400); self.assertIn("topic", r.get_json()["error"])   # a message, never an HTML 500
        r = c.post("/api/inbox/add", json={"headline": "Hand-added story", "url": "https://news.example/x", "pitch": "Pitch."},
                   base_url="http://127.0.0.1:42040")
        self.assertEqual(r.status_code, 200)
        self.assertIn("[PENDING] Hand-added story | https://news.example/x | Pitch.", self.inbox_file.read_text())

    def test_laptop_one_shot_setup_endpoints(self):
        import importlib, os, json, tempfile
        from unittest.mock import patch
        os.environ["NEWSROOM_NO_BACKGROUND"] = "1"
        app_mod = importlib.import_module("app")
        app_mod.cfg.clear(); app_mod.cfg.update(self.cfg)
        app_mod.room = core.Newsroom(app_mod.cfg)
        vs = Path(tempfile.mkdtemp()); (vs / "scripts" / "linux").mkdir(parents=True)
        (vs / "scripts" / "linux" / "setup-worker.sh").write_text('PC="@@PC@@"; PIN="@@PIN@@"\r\necho hi\r\n')
        (vs / "settings.json").write_text(json.dumps({"hf_token": "hf_abc", "deepseek_api_key": "sk-ds"}))
        app_mod.cfg.update(vs_repo=str(vs), mobile_pin="4711", laptop={"enabled": True, "ip": "192.0.2.9", "port": 42021})
        c = app_mod.app.test_client()
        lan = dict(base_url="http://192.168.1.20:42040", environ_base={"REMOTE_ADDR": "192.168.1.55"})
        self.assertEqual(c.get("/laptop/setup.sh", **lan).status_code, 403)   # passcode needed
        self.assertEqual(c.get("/laptop/keys?k=4711", base_url="http://8.8.8.8:42040", environ_base={"REMOTE_ADDR": "8.8.4.4"}).status_code, 403)
        body = c.get("/laptop/setup.sh?k=4711", **lan).get_data(as_text=True)
        self.assertIn('PC="192.168.1.20:42040"; PIN="4711"', body); self.assertNotIn("\r", body)
        with patch("newsroom.laptop.keyring", create=True):
            k = c.get("/laptop/keys?k=4711", **lan).get_json()
        self.assertEqual(k["hf_token"], "hf_abc"); self.assertNotIn("anthropic", json.dumps(k))
        cfgfile = vs / "config.json"
        with patch.object(core.config if hasattr(core, "config") else importlib.import_module("newsroom.config"), "path", return_value=cfgfile):
            from newsroom import config as ncfg
            with patch.object(ncfg, "path", return_value=cfgfile):
                r = c.post("/laptop/register?k=4711", json={"name": "lap.local", "ip": "192.0.2.10", "mac": "aa:bb:cc:dd:ee:ff",
                                                            "token": "A" * 24}, **lan).get_json()
        self.assertTrue(r["ok"]); self.assertFalse(r["reachable"])
        self.assertEqual((Path(self.cfg["state_file"]).parent / "laptop_token.txt").read_text(), "A" * 24)
        self.assertEqual(json.loads(cfgfile.read_text())["laptop"]["mac"], "aa:bb:cc:dd:ee:ff")
        self.assertEqual(c.post("/laptop/register?k=4711", json={"token": "x;rm"}, **lan).status_code, 400)


if __name__ == "__main__":
    unittest.main()


def test_brief_and_breaking_fields():
    from newsroom import inbox
    t = ("[PENDING] [BREAKING] Tesla x Alipay | https://ex.com/a | Pitch. Two. | RESEARCH BRIEF: background | with a pipe\n"
         "[PENDING] Plain | https://ex.com/b | Just pitch\n")
    items = inbox.active(t)
    tes = next(i for i in items if i["headline"] == "Tesla x Alipay")
    assert tes["breaking"] and tes["pitch"] == "Pitch. Two." and tes["brief"] == "background | with a pipe"
    assert not items[0]["brief"] and items[0]["headline"] == "Plain"
    # the [PROCESSED] line is written without the tag, and still closes the item
    assert not [i for i in inbox.active(t + inbox.processed_line(tes["headline"], "j1")) if i["headline"] == "Tesla x Alipay"]


def test_hold_file_pauses_new_work(tmp_path):
    from newsroom import core
    room = core.Newsroom({"state_file": str(tmp_path / "state.json"), "inbox_source": "file", "inbox_file": str(tmp_path / "i.txt"),
                          "video_studio": "http://127.0.0.1:9", "staging_dir": str(tmp_path / "s"), "video_studio_jobs": str(tmp_path / "j")})
    (tmp_path / "hold").write_text("")
    assert room.held() and room.explainer_next() is None and room.auto_next() is None


def test_one_new_video_per_hour(tmp_path):
    from newsroom import core
    room = core.Newsroom({"state_file": str(tmp_path / "state.json"), "inbox_source": "file", "inbox_file": str(tmp_path / "i.txt"),
                          "video_studio": "http://127.0.0.1:9", "staging_dir": str(tmp_path / "s"), "video_studio_jobs": str(tmp_path / "j")})
    assert not room.capped()
    room._started()
    assert room.capped() == "one new video per hour" and room.auto_next() is None


def test_publish_explainer_then_short_later(tmp_path):
    from newsroom import core
    room = core.Newsroom({"state_file": str(tmp_path / "state.json"), "inbox_source": "file", "inbox_file": str(tmp_path / "i.txt"),
                          "video_studio": "http://127.0.0.1:9", "staging_dir": str(tmp_path / "s"), "video_studio_jobs": str(tmp_path / "j"),
                          "channels": ["yousocial"]})
    room._set("x", status="published", kinds=["long", "short"], published={"long": "https://youtu.be/L"}, job="j1")
    import pytest
    with pytest.raises(ValueError): room.publish("x", "yousocial", kinds=["long"])   # already out
    calls = []
    core.bridge.pub_publish, orig = (lambda cfg, job, kinds, ch, vis, mode="new": calls.append(kinds) or {"tasks": {"short": "t"}}), core.bridge.pub_publish
    room._spawn = lambda *a: None
    try: room.publish("x", "yousocial")
    finally: core.bridge.pub_publish = orig
    assert calls == [["short"]]


def test_ten_videos_a_day(tmp_path):
    from newsroom import core
    room = core.Newsroom({"state_file": str(tmp_path / "state.json"), "inbox_source": "file", "inbox_file": str(tmp_path / "i.txt"),
                          "video_studio": "http://127.0.0.1:9", "staging_dir": str(tmp_path / "s"), "video_studio_jobs": str(tmp_path / "j"),
                          "min_minutes_between_videos": 0})
    for _ in range(9): room._started()
    assert not room.capped()
    room._started()
    assert "24 hours" in room.capped()


def test_state_import_and_counts(tmp_path):
    import json
    from newsroom import core
    (tmp_path / "state.json").write_text(json.dumps({"items": {"new1": {"status": "rendering"}}}))
    (tmp_path / "state_import.json").write_text(json.dumps({"items": {"new1": {"status": "failed"}, "old": {"status": "staged", "staged": "x"},
                                                                      "pub": {"status": "published", "published_at": 1}}}))
    room = core.Newsroom({"state_file": str(tmp_path / "state.json"), "inbox_source": "file", "inbox_file": str(tmp_path / "i.txt"),
                          "video_studio": "http://127.0.0.1:9", "staging_dir": str(tmp_path / "s"), "video_studio_jobs": str(tmp_path / "j")})
    assert room.item("new1")["status"] == "rendering" and room.item("old")["status"] == "staged"   # current state wins
    assert not (tmp_path / "state_import.json").exists()
    c = room.counts()
    assert (c["making"], c["to_review"], c["published"], c["published_24h"]) == (1, 1, 1, 0)


def test_single_instance_lock(tmp_path):
    import subprocess, sys, textwrap
    from newsroom import single
    assert single.acquire(tmp_path / "n.lock")
    code = textwrap.dedent(f"""
        import sys; sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})
        from newsroom import single
        print(single.acquire({str(tmp_path / 'n.lock')!r}))""")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).stdout.strip()
    assert out == "False"   # a second copy can't take it


def test_pause_resume_and_stalled_are_never_failed(tmp_path):
    from newsroom import core
    calls = []
    room = core.Newsroom({"state_file": str(tmp_path / "state.json"), "inbox_source": "file", "inbox_file": str(tmp_path / "i.txt"),
                          "video_studio": "http://127.0.0.1:9", "staging_dir": str(tmp_path / "s"), "video_studio_jobs": str(tmp_path / "j"),
                          "min_minutes_between_videos": 0})
    room._spawn = lambda *a: None
    orig = (core.bridge.vs_pause, core.bridge.vs_resume, core.bridge.vs_status)
    core.bridge.vs_pause = lambda cfg, j: calls.append(("pause", j))
    core.bridge.vs_resume = lambda cfg, j: calls.append(("resume", j))
    core.bridge.vs_status = lambda cfg: {"current": None}
    try:
        room._set("a", status="rendering", job="J1", headline="A")
        room.pause("a"); assert calls[-1] == ("pause", "J1")
        room._set("a", status="paused", paused_at=1)
        room._set("b", status="rendering", job="J2")
        import pytest
        with pytest.raises(ValueError): room.resume_job("a", auto=True)   # one at a time
        assert room.resume_job("a")["resume_req"] and room.item("a")["status"] == "paused"   # by hand: queued, not refused
        room._set("b", status="published")
        for _ in range(10): room._started()   # daily cap reached: a paused video still resumes
        assert room.capped() and room.auto_next() == "a" and calls[-1] == ("resume", "J1")
        assert room.item("a")["status"] == "rendering"
    finally:
        core.bridge.vs_pause, core.bridge.vs_resume, core.bridge.vs_status = orig


def test_breaking_pauses_an_ordinary_render(tmp_path):
    from newsroom import core
    (tmp_path / "i.txt").write_text("[PENDING] [BREAKING] Big news | https://ex.com/a | p\n")
    room = core.Newsroom({"state_file": str(tmp_path / "state.json"), "inbox_source": "file", "inbox_file": str(tmp_path / "i.txt"),
                          "video_studio": "http://127.0.0.1:9", "staging_dir": str(tmp_path / "s"), "video_studio_jobs": str(tmp_path / "j")})
    room.refresh_inbox()
    calls = []
    orig = core.bridge.vs_pause
    core.bridge.vs_pause = lambda cfg, j: calls.append(j)
    try:
        room._set("r", status="rendering", job="J9")
        room._started()   # inside the hourly gap: breaking still goes
        assert room.auto_next() is None and calls == ["J9"] and room.item("r")["pause_reason"] == "breaking news"
    finally:
        core.bridge.vs_pause = orig


def test_approve_several_then_publish_one_at_a_time(tmp_path):
    from newsroom import core
    room = core.Newsroom({"state_file": str(tmp_path / "state.json"), "inbox_source": "file", "inbox_file": str(tmp_path / "i.txt"),
                          "video_studio": "http://127.0.0.1:9", "staging_dir": str(tmp_path / "s"), "video_studio_jobs": str(tmp_path / "j"),
                          "channels": ["yousocial", "moonspots"]})
    room._spawn = lambda *a: None
    for k in ("a", "b", "c"): room._set(k, status="staged", staged="x", kinds=["short"], job="J" + k)
    room.approve("b", True, "moonspots", "Unlisted"); time.sleep(0.01); room.approve("a", True, "yousocial", "Private")
    sent = []
    orig = core.bridge.pub_publish
    core.bridge.pub_publish = lambda cfg, job, kinds, ch, vis, mode="new": sent.append((job, ch, vis)) or {"tasks": {"short": "t"}}
    try:
        q = room.publish_approved()
        assert sent == [("Jb", "moonspots", "Unlisted")] and q == ["a"]   # first one uploading, the next waits
        room.pump_publish(); assert len(sent) == 1                      # still uploading: the traffic cop waits
        room._set("b", status="published", published={"short": "u"})
        room.pump_publish()
        assert sent[-1] == ("Ja", "yousocial", "Private") and room.state["publish_queue"] == []
        assert not room.item("c").get("approved")
    finally:
        core.bridge.pub_publish = orig


def test_reject_writes_a_rejected_line_that_closes_the_item():
    from newsroom import inbox
    t = "[PENDING] Big story | https://ex.com/a | p\n" + inbox.rejected_line("Big story", "J1")
    assert inbox.active(t) == [] and inbox.parse(t)[1]["big story"]["rejected"] == "J1"


def test_feedback_teaches_house_style_and_reject_reason_too(tmp_path):
    from newsroom import core
    room = core.Newsroom({"state_file": str(tmp_path / "state.json"), "inbox_source": "file", "inbox_file": str(tmp_path / "i.txt"),
                          "video_studio": "http://127.0.0.1:9", "staging_dir": str(tmp_path / "s"), "video_studio_jobs": str(tmp_path / "j")})
    room._set("a", status="staged", staged="x", kinds=["short"], job="J1", headline="Nvidia earnings", mode="short")
    room._set("b", status="staged", staged="x", kinds=["short"], job="J2", headline="Tesla", mode="short")
    calls = []
    orig = core.bridge.vs_style_learn
    core.bridge.vs_style_learn = lambda cfg, note, ctx, job="": calls.append((note, ctx, job)) or {"added": [{"rule": "No puns."}], "replaced": []}
    try:
        r = room.feedback("a", "  the puns   were awful ")
        assert r["added"][0]["rule"] == "No puns." and calls[0] == ("the puns were awful", "short about 'Nvidia earnings'", "J1")
        assert room.item("a")["feedback"][0]["rules"] == ["No puns."]
        import pytest
        with pytest.raises(ValueError): room.feedback("a", "   ")
        room.reject("b", "too long and boring")
        for _ in range(50):
            if len(calls) > 1: break
            time.sleep(0.05)
        assert calls[1][0] == "Rejected: too long and boring" and room.item("b")["status"] == "rejected"
    finally:
        core.bridge.vs_style_learn = orig


def test_alternate_outlet_for_a_blocked_story():
    from newsroom import research as R
    rss = ('<rss><channel><item><title>Microsoft&#39;s AI tool diagnoses patients sequentially, like a doctor</title>'
           '<link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;url=https%3a%2f%2fwww.theverge.com%2fa%2fb&amp;c=1</link>'
           '<News:Source>The Verge</News:Source></item>'
           '<item><title>Microsoft develops AI tool that can diagnose sequentially like a doctor</title>'
           '<link>http://www.bing.com/news/apiclick.aspx?url=https%3a%2f%2fwww.reuters.com%2fx</link></item>'
           '<item><title>Apple ships a new iPhone</title><link>http://www.bing.com/news/apiclick.aspx?url=https%3a%2f%2fexample.com%2fz</link></item>'
           '</channel></rss>')
    alts = R.alternates("Microsoft develops AI tool that can diagnose 'sequentially' like a doctor", "www.reuters.com", get=lambda u: rss)
    assert alts == [("https://www.theverge.com/a/b", "Microsoft's AI tool diagnoses patients sequentially, like a doctor", "The Verge")]
    assert R.alternates("anything", get=lambda u: (_ for _ in ()).throw(OSError("offline"))) == []


def test_alternate_used_when_the_original_is_blocked(tmp_path):
    from newsroom import research
    room = core.Newsroom({"state_file": str(tmp_path / "s.json"), "staging_dir": str(tmp_path / "st"), "doc_id": ""})
    room._set("x", headline="Microsoft develops AI tool that can diagnose like a doctor", url="https://www.reuters.com/x", brief="")
    orig = research.fetch, research.alternates
    def fetch(u, cfg=None, log=print):
        if "reuters" in u: raise RuntimeError("couldn't read enough article text from www.reuters.com (0 words; HTTP 401)")
        return {"kind": "article", "url": u, "title": "t", "site": "The Verge", "text": "word " * 300, "words": 300}
    research.fetch = fetch
    research.alternates = lambda h, host="", limit=4: [("https://www.theverge.com/a", "t", "The Verge")]
    try:
        src = room._research("x")
        assert src["site"] == "The Verge" and room.item("x")["source_alt"] == "https://www.theverge.com/a"
    finally:
        research.fetch, research.alternates = orig
