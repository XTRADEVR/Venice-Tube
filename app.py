#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import hashlib, hmac, os, queue, re, shutil, socket, subprocess, sys, threading, time, uuid, zipfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
import streamlit as st
import yt_dlp

APP = "Venice Tube Grabber"
BASE = Path(__file__).resolve().parent
CFG_DIR = BASE / ".config"
CFG_FILE = CFG_DIR / "settings.json"
INCOMING = ".incoming"
ARCHIVE = ".downloaded.txt"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
FFMPEG = shutil.which("ffmpeg")
MEDIA_EXT = {".mp4",".mkv",".webm",".avi",".mov",".flv",".ts",".m4v",".mp3",".m4a",".opus",".ogg",".flac",".wav",".srt",".vtt",".ass",".jpg",".png",".webp"}
BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
IS_ANDROID = Path("/storage/emulated/0").exists() or Path("/sdcard").exists()

st.set_page_config(page_title=APP, page_icon="🎬", layout="centered")
# === PREMIUM DUAL THEME SWITCHER ===
BASE_THEME = Path(__file__).resolve().parent
theme = st.sidebar.radio("🎨 Tema Premium",["💎 Blue Shimmer", "🌑 Midnight Black"],horizontal=True,help="Midnight lebih hemat baterai & RAM")
css_name = "style_blue.css" if "Blue" in theme else "style_midnight.css"
css_path = BASE_THEME / css_name
if css_path.exists():
    st.markdown(f"<style>{css_path.read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)

try:
    import dns.message, dns.rdatatype, dns.resolver, requests
    HAS_DNS = True
except:
    HAS_DNS = False
CF_V4 = ["1.1.1.1","1.0.0.1"]
CF_DOH = "https://cloudflare-dns.com/dns-query"
_ORIG_GA = socket.getaddrinfo
_ORIG_GH = socket.gethostbyname

class DNSRouter:
    def __init__(self):
        self._cache = {}; self._lock = threading.Lock(); self._local = threading.local()
        self.mode = "system"; self.ipv4_only = True; self.ttl = 300.0; self._installed = False; self._resolver = None
        if HAS_DNS:
            self._resolver = dns.resolver.Resolver(configure=False)
            self._resolver.nameservers = CF_V4; self._resolver.timeout = 4.0; self._resolver.lifetime = 6.0
    def _doh(self, name, rdtype):
        q = dns.message.make_query(name, rdtype, use_edns=False)
        r = requests.post(CF_DOH, data=q.to_wire(), headers={"Content-Type": "application/dns-message","Accept": "application/dns-message","User-Agent": UA}, timeout=8)
        r.raise_for_status(); msg = dns.message.from_wire(r.content)
        return [rr.address for rs in msg.answer if rs.rdtype == rdtype for rr in rs]
    def _udp(self, name, rdtype):
        ans = self._resolver.resolve(name, rdtype, raise_on_no_answer=False)
        return [] if ans.rrset is None else [r.address for r in ans]
    def resolve(self, host, want4, want6):
        if not HAS_DNS or self._resolver is None: return []
        key = (host, int(want4), int(want6), self.mode); now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now: return list(hit[1])
        ips = []; types = ([dns.rdatatype.A] if want4 else []) + ([dns.rdatatype.AAAA] if want6 else [])
        for rdtype in types:
            try: ips.extend(self._doh(host, rdtype) if self.mode == "doh" else self._udp(host, rdtype))
            except: continue
        with self._lock:
            if len(self._cache) > 2000: self._cache.clear()
            self._cache[key] = (now + self.ttl, list(ips))
        return ips
    def _ga(self, host, port, family=0, type=0, proto=0, flags=0):
        orig = _ORIG_GA
        if self.mode == "system" or not HAS_DNS or host is None or isinstance(host, bytes) or getattr(self._local, "busy", False):
            return orig(host, port, family, type, proto, flags)
        host = str(host)
        for fam, probe in ((socket.AF_INET, socket.inet_pton),(socket.AF_INET6, socket.inet_pton)):
            try:
                probe(fam, host)
                return orig(host, port, family, type, proto, flags)
            except OSError: pass
        want6 = family == socket.AF_INET6 or (family == 0 and not self.ipv4_only)
        want4 = family in (0, socket.AF_INET)
        self._local.busy = True
        try: ips = self.resolve(host, want4, want6)
        finally: self._local.busy = False
        if not ips: return orig(host, port, family, type, proto, flags)
        socktype = type or socket.SOCK_STREAM; out = []
        for ip in ips:
            if ":" in ip:
                if family == socket.AF_INET: continue
                out.append((socket.AF_INET6, socktype, proto or 0, "", (ip, port or 0, 0, 0)))
            else:
                if family == socket.AF_INET6: continue
                out.append((socket.AF_INET, socktype, proto or 0, "", (ip, port or 0)))
        return out or orig(host, port, family, type, proto, flags)
    def _gh(self, host):
        if self.mode == "system" or not HAS_DNS: return _ORIG_GH(host)
        try:
            for fam,_,_,_,sa in self._ga(host, 0, socket.AF_INET, socket.SOCK_STREAM):
                if fam == socket.AF_INET: return sa[0]
        except: pass
        return _ORIG_GH(host)
    def install(self):
        if not self._installed and HAS_DNS:
            socket.getaddrinfo = self._ga; socket.gethostbyname = self._gh; self._installed = True
    def probe(self):
        if not HAS_DNS: return False, "dnspython belum terpasang"
        t0 = time.perf_counter(); ips = self.resolve("one.one.one.one", True, False); ms = (time.perf_counter() - t0) * 1000
        return (True, f"{ms:.0f} ms -> {ips[0]}") if ips else (False, "tidak ada jawaban")

def human(num):
    try: num = float(num)
    except: return "?"
    if num <= 0: return "0 B"
    for unit in ("B","KB","MB","GB","TB"):
        if num < 1024 or unit == "TB": return f"{num:.1f} {unit}"
        num /= 1024
    return "?"
def human_time(sec):
    try: sec = int(sec)
    except: return "--:--"
    if sec < 0: return "--:--"
    m,s = divmod(sec,60); h,m = divmod(m,60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
def default_dir(): return str(BASE / "downloads")
def prepare(root: Path):
    try:
        root.mkdir(parents=True, exist_ok=True); (root / INCOMING).mkdir(exist_ok=True)
        t = root / ".write_test"; t.write_bytes(b"1"); t.unlink(); return True, ""
    except Exception as exc: return False, str(exc)
def free_bytes(path: Path):
    try: return shutil.disk_usage(path).free
    except: return 0
def site_of(url: str):
    m = re.match(r"https?://([^/]+)", url or "")
    if not m: return "web"
    host = m.group(1).lower().split(":")[0]
    for pre in ("www.","m."):
        if host.startswith(pre): host = host[len(pre):]
    return BAD.sub("", host)[:40] or "web"
def sanitize(text, restrict=False, limit=100):
    text = BAD.sub(" ", text or ""); text = re.sub(r"\s+", " ", text).strip(" .")
    if restrict:
        text = re.sub(r"[^A-Za-z0-9 ._-]", "", text); text = re.sub(r"\s+", "_", text.strip())
    return text[:limit].rstrip(" .") or "video"
def unique_path(folder, stem, ext):
    ext = ext if ext.startswith(".") else f".{ext}"; cand = folder / f"{stem}{ext}"; i = 2
    while cand.exists(): cand = folder / f"{stem} ({i}){ext}"; i += 1
    return cand
def find_existing(folder, vid):
    if not vid: return None
    tag = f"[{sanitize(vid, True, 40)}]"
    try:
        for p in folder.iterdir():
            if p.is_file() and tag in p.name and not p.name.endswith((".part",".ytdl")): return p
    except: pass
    return None
def move_in(paths, dest):
    dest.mkdir(parents=True, exist_ok=True); moved = []
    for src in paths:
        try:
            target = unique_path(dest, src.stem, src.suffix); shutil.move(str(src), str(target)); moved.append(target)
        except: continue
    return moved
def collect_new(folder, since):
    out = []
    try:
        for p in folder.iterdir():
            if p.is_file() and p.stat().st_mtime >= since and not p.name.endswith(".part"): out.append(p)
    except: pass
    return out
def list_media(folder, site="", sort="terbaru"):
    items = []
    try:
        for p in folder.iterdir():
            if p.is_file() and p.suffix.lower() in MEDIA_EXT:
                if site and site not in p.name: continue
                items.append(p)
    except: return []
    keys = {"terbaru": lambda p: p.stat().st_mtime,"terlama": lambda p: p.stat().st_mtime,"terbesar": lambda p: p.stat().st_size,"nama": lambda p: p.name.lower()}
    return sorted(items, key=keys.get(sort, keys["terbaru"]), reverse=sort in ("terbaru","terbesar"))
def cleanup_old(folder, days):
    cutoff = time.time() - days * 86400; count = freed = 0
    try:
        for p in folder.iterdir():
            if p.is_file() and p.stat().st_mtime < cutoff:
                size = p.stat().st_size; p.unlink(); count += 1; freed += size
    except: pass
    return count, freed

DEFAULTS = {"dl_dir":"","dns_mode":"udp","ipv4_only":True,"fragments":4,"chunk":3145728,"rate":0.0,"parallel":1,"compat":True,"quality":"720","restrict":False,"audio_only":False,"subs":False,"thumb":False,"playlist":False,"insecure":False,"proxy":"","cookies":"","margin_mb":100,"quota_gb":2.0,"cleanup_days":30}
def load_cfg():
    import json; cfg = dict(DEFAULTS)
    try:
        if CFG_FILE.exists():
            raw = json.loads(CFG_FILE.read_text("utf-8"))
            if isinstance(raw, dict):
                for k,v in raw.items():
                    if k in DEFAULTS and isinstance(v, type(DEFAULTS[k])): cfg[k] = v
    except: pass
    if not cfg.get("dl_dir"): cfg["dl_dir"] = default_dir()
    return cfg
def save_cfg(cfg):
    import json
    try:
        CFG_DIR.mkdir(parents=True, exist_ok=True); tmp = CFG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2), "utf-8"); tmp.replace(CFG_FILE)
    except: pass
QUEUED,PROBING,DOWNLOADING,PROCESSING = "queued","probing","downloading","processing"
DONE,ERROR,CANCELLED,PAUSED,EXISTS,NO_SPACE = "done","error","cancelled","paused","exists","no_space"
ACTIVE = {PROBING,DOWNLOADING,PROCESSING}; FINISHED = {DONE,ERROR,CANCELLED,EXISTS,NO_SPACE}
HINTS = (("sign in to confirm","Butuh cookies"),("login","Butuh login"),("private video","Video privat"),("403","Diblokir"),("429","Rate limit"),("404","Link mati"),("unsupported url","Belum didukung"),("no space left","Ruang habis"),("ffmpeg","ffmpeg error"),("timed out","Timeout"),("connection","Koneksi gagal"),("geo","Geo block"))
def classify(msg):
    low = (msg or "").lower()
    for needle, hint in HINTS:
        if needle in low: return hint
    return "Buka log job"
class Cancelled(Exception): pass
@dataclass
class Job:
    url: str; fmt: str | None = None; id: str = field(default_factory=lambda: uuid.uuid4().hex[:10]); status: str = QUEUED; title: str = ""; site: str = ""; vid: str = ""; duration: float | None = None; total: int = 0; done: int = 0; speed: float = 0.0; eta: int | None = None; filepath: str | None = None; error: str | None = None; hint: str | None = None; log: deque = field(default_factory=lambda: deque(maxlen=300)); cancel: threading.Event = field(default_factory=threading.Event); skip: bool = False
class _Logger:
    def __init__(self, job): self.job = job
    def debug(self, msg):
        if not msg.startswith("[debug]"): self.job.log.append(msg)
    def info(self, msg): self.job.log.append(msg)
    def warning(self, msg): self.job.log.append(f"WARN: {msg}")
        def error(self, msg): self.job.log.append(f"ERROR: {msg}")

class JobManager:
    def __init__(self, cfg):
        self.cfg = dict(cfg); self._jobs = {}; self._lock = threading.RLock(); self._q = queue.Queue(); self._stop = threading.Event(); self._workers = []; self._target = max(1, min(int(cfg.get("parallel", 1) or 1), 4)); self.session_bytes = 0; self._ensure_workers()
    def update_cfg(self, cfg):
        self.cfg = dict(cfg); want = max(1, min(int(cfg.get("parallel", 1) or 1), 4))
        if want != self._target: self._target = want; self._ensure_workers()
    def _ensure_workers(self):
        with self._lock:
            self._workers = [w for w in self._workers if w.is_alive()]
            while len(self._workers) < self._target:
                t = threading.Thread(target=self._loop, daemon=True); t.start(); self._workers.append(t)
    def add(self, url, fmt=None):
        url = (url or "").strip()
        if not url.lower().startswith(("http://","https://")): return False, "URL harus http"
        with self._lock:
            for job in self._jobs.values():
                if job.url == url and job.status in ACTIVE | {QUEUED,PAUSED}: return False, "URL sudah ada"
            job = Job(url=url, fmt=fmt); self._jobs[job.id] = job
        self._q.put(job.id); return True, job.id
    def cancel(self, job_id):
        job = self._get(job_id)
        if job: job.cancel.set()
        if job and job.status == QUEUED: job.skip = True; job.status = CANCELLED
    def pause(self, job_id):
        job = self._get(job_id)
        if job and job.status in ACTIVE: job.cancel.set(); job.status = PAUSED
    def resume(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            if not job: return
            job.cancel = threading.Event(); job.skip = False; job.status = QUEUED; job.error = None
        self._q.put(job_id)
    def retry_failed(self):
        n = 0
        for job in list(self._jobs.values()):
            if job.status in {ERROR,NO_SPACE}: self.resume(job.id); n += 1
        return n
    def remove(self, job_id):
        with self._lock: job = self._jobs.pop(job_id, None)
        if job: job.cancel.set()
    def clear_finished(self):
        for job in list(self._jobs.values()):
            if job.status in FINISHED: self.remove(job.id)
    def _get(self, job_id):
        with self._lock: return self._jobs.get(job_id)
    def snapshot(self):
        with self._lock: jobs = list(self._jobs.values())
        return [{"id": j.id,"url": j.url,"status": j.status,"title": j.title or j.url,"total": j.total,"done": j.done,"speed": j.speed,"eta": j.eta,"filepath": j.filepath,"error": j.error,"hint": j.hint,"log": list(j.log)[-40:]} for j in jobs]
    def stats(self):
        with self._lock: jobs = list(self._jobs.values())
        return {"queued": sum(1 for j in jobs if j.status == QUEUED),"active": sum(1 for j in jobs if j.status in ACTIVE),"done": sum(1 for j in jobs if j.status == DONE),"failed": sum(1 for j in jobs if j.status == ERROR),"bytes": self.session_bytes}
    def _loop(self):
        while not self._stop.is_set():
            try: job_id = self._q.get(timeout=1.0)
            except queue.Empty: continue
            job = self._get(job_id)
            if not job or job.skip: continue
            try: self._run(job)
            except Cancelled: job.status = CANCELLED
            except Exception as exc: job.status = ERROR; job.error = str(exc); job.hint = classify(str(exc))
    def _opts(self, job, incoming):
        cfg = self.cfg
        opts = {"paths": {"home": str(incoming),"temp": str(incoming)},"outtmpl": {"default": "%(title).80B [%(id)s].%(ext)s"},"windowsfilenames": True,"restrictfilenames": bool(cfg.get("restrict")),"trim_file_name": 110,"overwrites": True,"continuedl": True,"updatetime": False,"quiet": True,"noprogress": True,"no_warnings": True,"retries": 15,"fragment_retries": 15,"socket_timeout": 30,"concurrent_fragment_downloads": int(cfg.get("fragments", 4)),"http_chunk_size": int(cfg.get("chunk", 3145728)),"geo_bypass": True,"nocheckcertificate": bool(cfg.get("insecure")),"http_headers": {"User-Agent": UA},"logger": _Logger(job),"progress_hooks": [self._hook(job)],"postprocessor_hooks": [self._pp(job)],"noplaylist": not bool(cfg.get("playlist")),"ignoreerrors": False}
        if cfg.get("proxy"): opts["proxy"] = cfg["proxy"]
        if cfg.get("rate"): opts["ratelimit"] = int(float(cfg["rate"]) * 1024 * 1024)
        if cfg.get("cookies") and Path(cfg["cookies"]).exists(): opts["cookiefile"] = cfg["cookies"]
        q = cfg.get("quality", "720")
        if cfg.get("audio_only"): opts["format"] = "ba/b"
        elif job.fmt: opts["format"] = job.fmt
        elif q == "best": opts["format"] = "bv*+ba/b"
        else: opts["format"] = f"bv*[height<={q}]+ba/b[height<={q}]/b"
        opts["merge_output_format"] = "mp4"; sort = [f"res:{q}"] if q != "best" else []
        if cfg.get("compat"): sort += ["vcodec:h264","acodec:aac","ext:mp4"]
        if sort: opts["format_sort"] = sort
        pps = []
        if cfg.get("audio_only"): pps.append({"key": "FFmpegExtractAudio","preferredcodec": "mp3","preferredquality": "192"})
        if cfg.get("subs"): opts.update({"writesubtitles": True,"writeautomaticsub": True,"subtitleslangs": ["id","en"],"subtitlesformat": "srt/best"})
        if cfg.get("thumb") and FFMPEG: opts["writethumbnail"] = True; pps.append({"key": "EmbedThumbnail","already_have_thumbnail": False})
        if pps: opts["postprocessors"] = pps
        return opts
    def _hook(self, job):
        def hook(d):
            if job.cancel.is_set(): raise Cancelled()
            if d.get("status") == "downloading":
                job.status = DOWNLOADING; job.done = int(d.get("downloaded_bytes") or 0); job.total = int(d.get("total_bytes") or d.get("total_bytes_estimate") or 0); job.speed = float(d.get("speed") or 0.0); job.eta = d.get("eta")
            elif d.get("status") == "finished": job.status = PROCESSING
        return hook
    def _pp(self, job):
        def hook(d):
            if job.cancel.is_set(): raise Cancelled()
            if d.get("status") == "started": job.status = PROCESSING
        return hook
    def _run(self, job):
        root = Path(self.cfg["dl_dir"]).expanduser(); incoming = root / INCOMING
        root.mkdir(parents=True, exist_ok=True); incoming.mkdir(parents=True, exist_ok=True)
        job.site = site_of(job.url); job.status = PROBING
        probe = {"quiet": True,"noprogress": True,"no_warnings": True,"skip_download": True,"socket_timeout": 30,"noplaylist": True,"http_headers": {"User-Agent": UA},"logger": _Logger(job)}
        if self.cfg.get("proxy"): probe["proxy"] = self.cfg["proxy"]
        if self.cfg.get("cookies") and Path(self.cfg["cookies"]).exists(): probe["cookiefile"] = self.cfg["cookies"]
        with yt_dlp.YoutubeDL(probe) as ydl: info = ydl.extract_info(job.url, download=False) or {}
        if info.get("_type") == "playlist":
            entries = [e for e in (info.get("entries") or []) if e]; info = entries[0] if entries else info
        job.title = info.get("title") or job.url; job.vid = str(info.get("id") or ""); job.duration = info.get("duration")
        existing = find_existing(root, job.vid)
        if existing: job.filepath = str(existing); job.status = EXISTS; return
        needed = 0
        for f in info.get("formats") or []:
            size = f.get("filesize") or f.get("filesize_approx")
            if size: needed = max(needed, int(size))
        margin = int(float(self.cfg.get("margin_mb", 100)) * 1024 * 1024)
        if free_bytes(root) < needed + margin: job.status = NO_SPACE; job.hint = f"Perlu {human(needed)} + margin {human(margin)}"; return
        quota = float(self.cfg.get("quota_gb", 0) or 0) * 1024 ** 3
        if quota and self.session_bytes >= quota: job.status = QUEUED; job.hint = "Kuota sesi tercapai"; self._q.put(job.id); time.sleep(2); return
        if job.cancel.is_set(): raise Cancelled()
        started = time.time() - 1; job.status = DOWNLOADING
        with yt_dlp.YoutubeDL(self._opts(job, incoming)) as ydl: ydl.download([job.url])
        produced = collect_new(incoming, started)
        if not produced: job.status = ERROR; job.error = "Tidak ada file dihasilkan"; job.hint = classify(job.log[-1] if job.log else ""); return
        moved = move_in(produced, root)
        if not moved: job.status = ERROR; job.error = "Gagal memindahkan file"; return
        job.filepath = str(moved[0]); job.status = DONE
        try: self.session_bytes += sum(p.stat().st_size for p in moved)
        except: pass

def expected_password():
    try: secret = st.secrets.get("APP_PASSWORD")
    except: secret = None
    return (secret or os.environ.get("APP_PASSWORD") or "").strip()
def login_gate():
    want = expected_password()
    if not want or st.session_state.get("authed"): return True
    st.title("🎬 " + APP)
    with st.form("login"):
        pwd = st.text_input("Password", type="password")
        if st.form_submit_button("Masuk", use_container_width=True):
            if hmac.compare_digest(hashlib.sha256(pwd.encode()).hexdigest(), hashlib.sha256(want.encode()).hexdigest()):
                st.session_state["authed"] = True; st.rerun()
            st.error("Password salah.")
    return False

def main():
    ROUTER.install()
    if "cfg" not in st.session_state: st.session_state["cfg"] = load_cfg()
    cfg = st.session_state["cfg"]; ROUTER.mode = cfg.get("dns_mode", "udp"); ROUTER.ipv4_only = bool(cfg.get("ipv4_only", True))
    if not login_gate(): return
    if "mgr" not in st.session_state: st.session_state["mgr"] = JobManager(cfg)
    mgr = st.session_state["mgr"]; mgr.update_cfg(cfg)
    with st.sidebar:
        st.header("Jaringan")
        if HAS_DNS:
            mode = st.radio("Resolver", ["udp","doh","system"], index=["udp","doh","system"].index(cfg.get("dns_mode", "udp")), format_func=lambda m: {"udp":"Cloudflare 1.1.1.1","doh":"Cloudflare DoH","system":"Sistem"}[m])
            cfg["dns_mode"] = mode; ROUTER.mode = mode
            cfg["ipv4_only"] = st.checkbox("IPv4 saja", value=bool(cfg.get("ipv4_only", True))); ROUTER.ipv4_only = cfg["ipv4_only"]
            if st.button("Uji resolver", use_container_width=True): ok,msg = ROUTER.probe(); (st.success if ok else st.error)(msg)
        else: st.warning("dnspython tidak terpasang")
        st.divider(); st.header("Penyimpanan")
        cfg["dl_dir"] = st.text_input("Folder", value=cfg["dl_dir"])
        c1,c2 = st.columns(2)
        if c1.button("Kartu SD", use_container_width=True): cfg["dl_dir"] = "/storage/emulated/0/Download/VeniceGrab"; st.rerun()
        if c2.button("Lokal", use_container_width=True): cfg["dl_dir"] = str(BASE / "downloads"); st.rerun()
        if not IS_ANDROID: st.caption("Non-Android: file hilang saat restart")
        cfg["margin_mb"] = st.number_input("Margin aman (MB)", 50, 5000, int(cfg["margin_mb"]), 50)
        cfg["quota_gb"] = st.number_input("Kuota sesi (GB)", 0.0, 500.0, float(cfg["quota_gb"]), 1.0)
        st.divider(); st.header("Kecepatan & format")
        cfg["parallel"] = st.slider("Unduhan paralel", 1, 4, int(cfg["parallel"])); cfg["fragments"] = st.slider("Fragmen paralel", 1, 32, int(cfg["fragments"]))
        cfg["chunk"] = st.select_slider("Chunk HTTP", [1048576,3145728,5242880,10485760,20971520], value=int(cfg["chunk"]), format_func=human)
        cfg["rate"] = st.number_input("Batas MB/s (0=bebas)", 0.0, 100.0, float(cfg["rate"]), 1.0)
        cfg["quality"] = st.selectbox("Batas resolusi", ["best","1080","720","480","360"], index=["best","1080","720","480","360"].index(cfg["quality"]))
        cfg["compat"] = st.checkbox("Prioritaskan H.264/MP4", value=bool(cfg["compat"])); cfg["audio_only"] = st.checkbox("Audio MP3 saja", value=bool(cfg["audio_only"]))
        cfg["subs"] = st.checkbox("Subtitle", value=bool(cfg["subs"])); cfg["thumb"] = st.checkbox("Thumbnail", value=bool(cfg["thumb"]))
        cfg["playlist"] = st.checkbox("Ikuti playlist", value=bool(cfg["playlist"])); cfg["restrict"] = st.checkbox("Nama file ASCII", value=bool(cfg["restrict"]))
        st.divider(); st.header("Lainnya")
        cfg["proxy"] = st.text_input("Proxy", value=cfg["proxy"]); cfg["insecure"] = st.checkbox("Abaikan sertifikat TLS", value=bool(cfg["insecure"]))
        up = st.file_uploader("cookies.txt", type=["txt"])
        if up is not None:
            CFG_DIR.mkdir(parents=True, exist_ok=True); target = CFG_DIR / "cookies.txt"; target.write_bytes(up.getvalue())
            try: target.chmod(0o600)
            except: pass
            cfg["cookies"] = str(target)
        if cfg.get("cookies") and Path(cfg["cookies"]).exists():
            st.caption(f"Cookies aktif: {Path(cfg['cookies']).name}")
            if st.button("Hapus cookies", use_container_width=True):
                try: Path(cfg["cookies"]).unlink()
                except: pass
                cfg["cookies"] = ""; st.rerun()
        st.divider(); st.caption(f"ffmpeg: {'ada' if FFMPEG else 'TIDAK ADA'}")
    save_cfg(cfg); root = Path(cfg["dl_dir"]).expanduser(); ok_dir, dir_err = prepare(root)
    st.title("🎬 " + APP)
    if ok_dir: st.caption(f"`{root}` - kosong {human(free_bytes(root))} - DNS {cfg['dns_mode']} - paralel {cfg['parallel']}")
    else: st.error(f"Folder tidak siap: {dir_err}")
    with st.form("add", clear_on_submit=True):
        url = st.text_input("Link", placeholder="https://..."); a,b = st.columns([1,3])
        do_probe = a.form_submit_button("Cek format", use_container_width=True); do_add = b.form_submit_button("Tambah ke antrean", type="primary", use_container_width=True)
        if do_add and url: good,msg = mgr.add(url); (st.success if good else st.warning)("Masuk antrean." if good else msg)
        if do_probe and url: st.session_state["probe_url"] = url
    bulk = st.text_area("Tambah massal (satu per baris)", height=90)
    if st.button("Masukkan semua", disabled=not bulk.strip()):
        n = sum(1 for line in bulk.splitlines() if line.strip() and mgr.add(line.strip())[0]); st.success(f"{n} link masuk antrean.")
    probe_url = st.session_state.get("probe_url")
    if probe_url:
        try:
            with st.spinner("Mengambil metadata..."):
                with yt_dlp.YoutubeDL({"quiet": True,"no_warnings": True,"noprogress": True,"skip_download": True,"noplaylist": True}) as ydl:
                    info = ydl.extract_info(probe_url, download=False) or {}
            st.markdown(f"**{info.get('title', '(tanpa judul)')}** - {human_time(info.get('duration'))}")
            rows = [{"id": f.get("format_id"),"res": f.get("resolution") or "-","ext": f.get("ext"),"size": human(f.get("filesize") or f.get("filesize_approx"))} for f in (info.get("formats") or []) if f.get("vcodec") != "none" or f.get("acodec") != "none"]
            pick = st.selectbox("Format", ["auto"] + [f"{r['id']} - {r['res']} - {r['ext']} - {r['size']}" for r in rows])
            if st.button("Tambah dengan format ini", type="primary"):
                fmt = None if pick == "auto" else pick.split(" - ")[0]; good,msg = mgr.add(probe_url, fmt); (st.success if good else st.warning)("Masuk antrean." if good else msg)
        except Exception as exc: st.error(f"Gagal probe: {exc}")
    st.divider(); st.subheader("Antrean")
    @st.fragment(run_every="1.2s")
    def queue_view():
        s = mgr.stats(); st.caption(f"antre {s['queued']} - aktif {s['active']} - selesai {s['done']} - gagal {s['failed']} - terunduh {human(s['bytes'])}")
        jobs = mgr.snapshot()
        if not jobs: st.caption("Antrean kosong.")
        for job in jobs:
            icon = {"done": "✅","error": "⛔","cancelled": "⏹","exists": "♻️","no_space": "💾","paused": "⏸","queued": "🕒"}.get(job["status"], "⬇️")
            with st.container(border=True):
                st.markdown(f"{icon} **{job['title'][:110]}**")
                if job["status"] == "downloading":
                    total = job["total"] or 0; frac = (job["done"] / total) if total else 0.0
                    st.progress(min(max(frac, 0.0), 1.0)); st.caption(f"{human(job['done'])} / {human(total) if total else '?'} - {human(job['speed'])}/s - sisa {human_time(job['eta'])}")
                elif job["status"] == "processing": st.info("Memproses file...")
                elif job["status"] == "error":
                    st.error(job["error"])
                    if job["hint"]: st.caption("Saran: " + job["hint"])
                elif job["status"] == "exists": st.caption("Sudah ada di folder.")
                elif job["status"] == "no_space": st.warning("Ruang tidak cukup. " + (job["hint"] or ""))
                cols = st.columns(5)
                if job["status"] in ACTIVE | {QUEUED}:
                    if cols[0].button("Batal", key=f"c{job['id']}", use_container_width=True): mgr.cancel(job["id"])
                if job["status"] in {"error","cancelled","paused","no_space"}:
                    if cols[1].button("Ulangi", key=f"r{job['id']}", use_container_width=True): mgr.resume(job["id"])
                if job["status"] == "downloading":
                    if cols[2].button("Jeda", key=f"p{job['id']}", use_container_width=True): mgr.pause(job["id"])
                if job["status"] in FINISHED:
                    if cols[3].button("Hapus", key=f"x{job['id']}", use_container_width=True): mgr.remove(job["id"])
                with cols[4].popover("Log", use_container_width=True): st.code("\n".join(job["log"]) or "(kosong)")
        c1,c2 = st.columns(2)
        if c1.button("Ulangi semua gagal", use_container_width=True): st.toast(f"{mgr.retry_failed()} job diulang")
        if c2.button("Bersihkan selesai", use_container_width=True): mgr.clear_finished()
    queue_view()
    st.divider(); st.subheader("File")
    if ok_dir:
        files = list_media(root)[:80]
        if not files: st.caption("Belum ada file.")
        for p in files:
            try: size = p.stat().st_size
            except: continue
            with st.container(border=True):
                st.markdown(f"`{p.name}`  \n{human(size)}")
                c1,c2,c3 = st.columns(3)
                # === LIMITER 2GB SMART - FINAL FIX BARIS 788 ===
                IS_CLOUD = os.path.exists("/mount/src") or os.path.exists("/mount/app")
                LIMIT_MB = 450 if IS_CLOUD else 2048
                size_mb = size / (1024*1024)
                if size_mb > LIMIT_MB:
                    if IS_CLOUD:
                        c1.error(f"{size_mb:.0f} MB > {LIMIT_MB} MB")
                        c1.caption("⚠️ Cloud limit. Pakai VPS/Docker untuk 2GB")
                    else:
                        c1.error(f"{size_mb:.0f} MB > 2GB")
                else:
                    with open(p, "rb") as fh:
                        c1.download_button("Simpan ke HP", data=fh, file_name=p.name, mime="application/octet-stream", key=f"d{p.name}{size}", use_container_width=True)
                with c2.popover("Pratinjau", use_container_width=True):
                    if p.suffix.lower() in {".mp4",".mkv",".webm",".mov"}: st.video(str(p))
                    elif p.suffix.lower() in {".jpg",".png",".webp"}: st.image(str(p))
                    else: st.caption("Tidak ada pratinjau.")
                if c3.button("Hapus", key=f"del{p.name}{size}", use_container_width=True):
                    try: p.unlink(); st.rerun()
                    except Exception as exc: st.error(str(exc))
        with st.expander("Aksi massal"):
            chosen = st.multiselect("Pilih file", [p.name for p in files])
            dest = st.text_input("Folder tujuan", value="/storage/emulated/0/Movies/VeniceGrab")
            b1,b2 = st.columns(2)
            if b1.button("Pindahkan", use_container_width=True) and chosen:
                moved = move_in([root / n for n in chosen if (root / n).exists()], Path(dest).expanduser()); st.success(f"{len(moved)} file dipindahkan.")
            if b2.button("Zip", use_container_width=True) and chosen:
                # === LIMITER ZIP 2GB SMART - FINAL FIX BARIS 811 ===
                total_bytes = sum((root / n).stat().st_size for n in chosen if (root / n).exists())
                total_mb = total_bytes / (1024*1024)
                IS_CLOUD_ZIP = os.path.exists("/mount/src") or os.path.exists("/mount/app")
                LIMIT_MB_ZIP = 450 if IS_CLOUD_ZIP else 2048
                if total_mb > LIMIT_MB_ZIP:
                    st.error(f"Total {total_mb:.0f} MB melebihi limit {LIMIT_MB_ZIP} MB")
                    if IS_CLOUD_ZIP: st.warning("💰 File 2GB butuh VPS/Docker. Cloud cuma 450MB. Jual paket Sultan!")
                    else: st.warning("File melebihi 2GB, pecah jadi 2 zip.")
                    st.stop()
                zpath = root / "arsip.zip"
                with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as zf:
                    for n in chosen:
                        src = root / n
                        if src.exists(): zf.write(src, src.name)
                st.success(f"Zip dibuat. {human(total_bytes)}")
    st.divider()
    with st.expander("Perawatan"):
        try:
            import yt_dlp as _y; ver = _y.version.__version__
        except: ver = "?"
        st.write(f"yt-dlp terpasang: `{ver}`")
        if st.button("Update yt-dlp"):
            with st.spinner("Memperbarui..."):
                proc = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--user", "-U", "yt-dlp"], capture_output=True, text=True, timeout=300)
            if proc.returncode == 0: st.success("Diperbarui. Reboot app agar aktif.")
            else: st.error(proc.stderr[-600:])
        cfg["cleanup_days"] = st.number_input("Hapus file lebih tua dari (hari)", 1, 365, int(cfg["cleanup_days"]))
        if st.button("Bersihkan file lama"):
            n,freed = cleanup_old(root, int(cfg["cleanup_days"])); st.success(f"{n} file dihapus, {human(freed)} dibebaskan.")
        if st.button("Reset arsip duplikat"):
            try: (root / ARCHIVE).unlink(); st.success("Arsip dihapus.")
            except: st.caption("Arsip belum ada.")

ROUTER = DNSRouter()
if __name__ == "__main__":
    main()
