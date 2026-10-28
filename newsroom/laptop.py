"""The Linux laptop as an overflow worker (a full copy of Video Studio, Hugging Face images only, never publishes).
Finding it on the home network, in order: its .local name (Ubuntu announces it), the last address that worked, the
configured address, then a ping sweep of the PC's subnet and a lookup of its MAC address in the ARP table.
It is used only when it answers, is on AC power and is idle."""
import concurrent.futures as cf
import json
import time
import os
import re
import subprocess
from pathlib import Path

import requests

from . import bridge


def token(cfg):
    if cfg.get("laptop_token"): return cfg["laptop_token"]
    try: return (Path(cfg["state_file"]).parent / "laptop_token.txt").read_text(encoding="utf-8").strip()
    except OSError: return ""


def _probe(host, port, tok):
    try:
        r = requests.get(f"http://{host}:{port}/api/status", headers={"X-VS-Token": tok}, timeout=3)
        if r.ok and r.json().get("worker"): return r.json()
    except (requests.RequestException, ValueError):
        pass
    return None


def _mac_ip(mac, subnet):
    """Ping the /24 so the ARP table fills, then find the laptop's MAC in it. Windows only."""
    if os.name != "nt" or not mac or not subnet: return None
    want = mac.lower().replace(":", "-")
    def ping(i):
        subprocess.run(["ping", "-n", "1", "-w", "300", f"{subnet}.{i}"], capture_output=True, creationflags=0x08000000)
    with cf.ThreadPoolExecutor(64) as ex: list(ex.map(ping, range(1, 255)))
    out = subprocess.run(["arp", "-a"], capture_output=True, text=True, creationflags=0x08000000).stdout
    for line in out.splitlines():
        m = re.match(r"\s*(\d+\.\d+\.\d+\.\d+)\s+([0-9a-f-]{17})", line, re.I)
        if m and m.group(2).lower() == want: return m.group(1)
    return None


def find(cfg, state):
    """-> (base_url, status) or (None, None). Remembers the address that worked in state["laptop_ip"]."""
    lc = cfg.get("laptop") or {}
    if not lc.get("enabled"): return None, None
    port, tok = int(lc.get("port", 42021)), token(cfg)
    if not tok: return None, None
    hosts = [h for h in (lc.get("name"), state.get("laptop_ip"), lc.get("ip")) if h]
    for h in dict.fromkeys(hosts):
        st = _probe(h, port, tok)
        if st: return _use(h, port, tok, state, st)
    ip = _mac_ip(lc.get("mac"), lc.get("subnet") or ".".join((lc.get("ip") or "...").split(".")[:3]))
    if ip:
        st = _probe(ip, port, tok)
        if st: return _use(ip, port, tok, state, st)
    return None, None


def _use(host, port, tok, state, st):
    base = f"http://{host}:{port}"
    bridge.TOKENS[base] = tok
    if re.match(r"\d+\.\d+\.\d+\.\d+$", host): state["laptop_ip"] = host
    return base, st


def ready(st):
    """Idle, and plugged in (or no battery information)."""
    p = (st or {}).get("power")
    return bool(st) and not st.get("current") and (p is None or p.get("ac"))


def keys(cfg):
    """DeepSeek key (Windows vault "ExplainerStudio", else Video Studio's settings) and the Hugging Face token."""
    vs = Path(cfg.get("vs_repo") or r"F:\code\video-studio-test") / "settings.json"
    try: st = json.loads(vs.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError): st = {}
    ds = ""
    try:
        import keyring
        ds = keyring.get_password("ExplainerStudio", "deepseek_api_key") or ""
    except Exception:
        pass
    if not ds and st.get("deepseek_api_key") not in (None, "", "vault"): ds = st["deepseek_api_key"]
    return {"deepseek_api_key": ds, "hf_token": st.get("hf_token") or ""}


def register(cfg, room, d):
    """Save the laptop's token, MAC and address, then find it the normal way and report what the Newsroom sees."""
    tok, mac, ip, name = (str(d.get(k) or "").strip() for k in ("token", "mac", "ip", "name"))
    if not re.fullmatch(r"[A-Za-z0-9]{16,64}", tok): raise ValueError("bad token")
    if mac and not re.fullmatch(r"[0-9a-fA-F:]{17}", mac): raise ValueError("bad MAC address")
    folder = Path(cfg["state_file"]).parent
    (folder / "laptop_token.txt").write_text(tok, encoding="utf-8")
    from . import config as _config
    p = _config.path()
    try: raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError): raw = {}
    lc = raw.setdefault("laptop", {})
    lc.update({k: v for k, v in (("mac", mac), ("ip", ip), ("name", name)) if v}); lc.setdefault("enabled", True)
    lc.setdefault("port", 42021); lc.setdefault("backlog", 1)
    p.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    cfg["laptop"] = {**(cfg.get("laptop") or {}), **lc}; cfg.pop("laptop_token", None)
    base, st = find(cfg, room.state)
    room.state["laptop_registered"] = {"at": time.time(), "name": name, "ip": ip, "mac": mac, "seen": bool(base)}
    room.log(f"Laptop registered: {name} {ip} {mac}; " + (f"reachable at {base}, " + ("ready" if ready(st) else "busy or on battery") if base else "NOT reachable yet"))
    room.save()
    return {"ok": True, "reachable": bool(base), "url": base, "ready": ready(st) if base else False,
            "power": (st or {}).get("power"), "worker": (st or {}).get("worker")}


AUDIO = (".mp3", ".m4a", ".wav", ".ogg", ".aac", ".flac")


def music_dir(cfg):
    """Video Studio's music folder: settings music_dir, else <jobs_dir parent>\\music."""
    vs = Path(cfg.get("vs_repo") or r"F:\code\video-studio-test") / "settings.json"
    try: st = json.loads(vs.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError): st = {}
    if st.get("music_dir"): return Path(st["music_dir"])
    return Path(st.get("jobs_dir") or r"F:\videos_test\jobs").parent / "music"


def music_tar(cfg):
    """-> BytesIO of library.json + hook/*, bed/* audio (no screenshots, logs or usage counters), or None if there is no library."""
    import io
    import tarfile
    root = music_dir(cfg)
    files = [root / "library.json"] if (root / "library.json").is_file() else []
    for kind in ("hook", "bed"):
        d = root / kind
        files += [f for f in (sorted(d.iterdir()) if d.is_dir() else []) if f.is_file() and f.suffix.lower() in AUDIO]
    if not files: return None
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for f in files: t.add(f, arcname=f.relative_to(root).as_posix())
    buf.seek(0)
    return buf
