"""Manajer antrean unduhan: worker thread, batal, jeda, dedupe, guard disk."""
from __future__ import annotations

import queue
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import yt_dlp

import storage

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

QUEUED = "queued"
PROBING = "probing"
DOWNLOADING = "downloading"
PROCESSING = "processing"
DONE = "done"
ERROR = "error"
CANCELLED = "cancelled"
PAUSED = "paused"
EXISTS = "exists"
NO_SPACE = "no_space"

ACTIVE = {PROBING, DOWNLOADING, PROCESSING}
FINISHED = {DONE, ERROR, CANCELLED, EXISTS, NO_SPACE}

HINTS = (
    ("sign in to confirm", "auth", "Butuh cookies: ekspor cookies.txt domain ini lalu unggah di sidebar."),
    ("login", "auth", "Halaman butuh login. Pakai cookies.txt."),
    ("private video", "auth", "Video privat — perlu akun berlangganan."),
    ("403", "blocked", "Diblokir server. Nyalakan penyamaran TLS atau ganti DNS/proxy."),
    ("429", "blocked", "Kena rate limit. Turunkan fragmen atau tunggu."),
    ("404", "gone", "Link mati atau sudah dihapus."),
    ("unsupported url", "extractor", "Situs belum didukung. Coba update yt-dlp."),
    ("no space left", "disk", "Ruang penyimpanan habis."),
    ("ffmpeg", "ffmpeg", "ffmpeg bermasalah. Cek pemasangan ffmpeg."),
    ("timed out", "network", "Koneksi timeout. Cek jaringan/proxy."),
    ("connection", "network", "Koneksi gagal. Cek jaringan."),
    ("geo", "geo", "Dibatasi wilayah. Coba proxy negara lain."),
)


class Cancelled(Exception):
    pass


def classify(msg: str) -> tuple[str, str]:
    low = (msg or "").lower()
    for needle, cat, hint in HINTS:
        if needle in low:
            return cat, hint
    return "unknown", "Buka log job untuk detail."


@dataclass
class Job:
    url: str
    fmt: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    status: str = QUEUED
    title: str = ""
    site: str = ""
    vid: str = ""
    duration: float | None = None
    total: int = 0
    done: int = 0
    speed: float = 0.0
    eta: int | None = None
    filepath: str | None = None
    error: str | None = None
    category: str | None = None
    hint: str | None = None
    created: float = field(default_factory=time.time)
    log: deque = field(default_factory=lambda: deque(maxlen=300))
    cancel: threading.Event = field(default_factory=threading.Event)
    skip: bool = False


class _Logger:
    def __init__(self, job: Job) -> None:
        self.job = job

    def debug(self, msg: str) -> None:
        if not msg.startswith("[debug]"):
            self.job.log.append(msg)

    def info(self, msg: str) -> None:
        self.job.log.append(msg)

    def warning(self, msg: str) -> None:
        self.job.log.append(f"WARN: {msg}")

    def error(self, msg: str) -> None:
        self.job.log.append(f"ERROR: {msg}")


class JobManager:
    def __init__(self, cfg: dict) -> None:
        self.cfg = dict(cfg)
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()
        self._q: queue.Queue[str] = queue.Queue()
        self._stop = threading.Event()
        self._workers: list[threading.Thread] = []
        self._target = max(1, min(int(cfg.get("parallel", 2) or 2), 4))
        self.session_bytes = 0
        self.alerts: deque = deque(maxlen=40)
        self._ensure_workers()

    # --------------------------- infrastruktur --------------------------- #
    def update_cfg(self, cfg: dict) -> None:
        self.cfg = dict(cfg)
        want = max(1, min(int(cfg.get("parallel", 2) or 2), 4))
        if want != self._target:
            self._target = want
            self._ensure_workers()

    def _ensure_workers(self) -> None:
        with self._lock:
            alive = [w for w in self._workers if w.is_alive()]
            self._workers = alive
            while len(alive) < self._target:
                t = threading.Thread(target=self._loop, daemon=True, name=f"dl-{len(alive)}")
                t.start()
                alive.append(t)
                self._workers.append(t)

    def shutdown(self) -> None:
        self._stop.set()
        for job in self._snapshot_jobs():
            job.cancel.set()

    def alert(self, text: str) -> None:
        self.alerts.append(text)

    # ------------------------------ antrean ----------------------------- #
    def add(self, url: str, fmt: str | None = None) -> tuple[bool, str]:
        url = (url or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            return False, "URL harus diawali http:// atau https://"
        with self._lock:
            for job in self._jobs.values():
                if job.url == url and job.status in ACTIVE | {QUEUED, PAUSED}:
                    return False, "URL sudah ada di antrean"
            job = Job(url=url, fmt=fmt)
            self._jobs[job.id] = job
            self._order.append(job.id)
        self._q.put(job.id)
        return True, job.id

    def cancel(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
        if not job:
            return
        job.cancel.set()
        if job.status == QUEUED:
            job.skip = True
            job.status = CANCELLED

    def pause(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
        if job and job.status in ACTIVE:
            job.cancel.set()
            job.status = PAUSED

    def resume(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.cancel = threading.Event()
            job.skip = False
            job.status = QUEUED
            job.error = None
        self._q.put(job_id)

    def retry_failed(self) -> int:
        n = 0
        for job in self._snapshot_jobs():
            if job.status in {ERROR, NO_SPACE}:
                self.resume(job.id)
                n += 1
        return n

    def remove(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.pop(job_id, None)
            if job_id in self._order:
                self._order.remove(job_id)
        if job:
            job.cancel.set()

    def clear_finished(self) -> None:
        for job in self._snapshot_jobs():
            if job.status in FINISHED:
                self.remove(job.id)

    # ----------------------------- pembacaan ---------------------------- #
    def _snapshot_jobs(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

    def snapshot(self) -> list[dict]:
        out = []
        for job in self._snapshot_jobs():
            out.append(
                {
                    "id": job.id,
                    "url": job.url,
                    "status": job.status,
                    "title": job.title or job.url,
                    "site": job.site,
                    "total": job.total,
                    "done": job.done,
                    "speed": job.speed,
                    "eta": job.eta,
                    "filepath": job.filepath,
                    "error": job.error,
                    "category": job.category,
                    "hint": job.hint,
                    "duration": job.duration,
                    "log": list(job.log)[-40:],
                }
            )
        return out

    def stats(self) -> dict:
        jobs = self._snapshot_jobs()
        return {
            "queued": sum(1 for j in jobs if j.status == QUEUED),
            "active": sum(1 for j in jobs if j.status in ACTIVE),
            "done": sum(1 for j in jobs if j.status == DONE),
            "failed": sum(1 for j in jobs if j.status == ERROR),
            "session_bytes": self.session_bytes,
        }

    # ------------------------------ worker ------------------------------ #
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self._q.get(timeout=1.0)
            except queue.Empty:
                with self._lock:
                    if len([w for w in self._workers if w.is_alive()]) > self._target:
                        return
                continue
            with self._lock:
                job = self._jobs.get(job_id)
            if not job or job.skip:
                continue
            try:
                self._run(job)
            except Exception as exc:
                job.status = ERROR
                job.error = str(exc)
                job.category, job.hint = classify(str(exc))

    # ------------------------------ proses ------------------------------ #
    def _opts(self, job: Job, incoming: Path) -> dict:
        cfg = self.cfg
        opts: dict = {
            "paths": {"home": str(incoming), "temp": str(incoming)},
            "outtmpl": {"default": "%(title).120B [%(id)s].%(ext)s"},
            "windowsfilenames": True,
            "restrictfilenames": bool(cfg.get("restrict")),
            "trim_file_name": 110,
            "overwrites": False,
            "continuedl": True,
            "updatetime": False,
            "quiet": True,
            "noprogress": True,
            "no_warnings": True,
            "retries": 15,
            "fragment_retries": 15,
            "socket_timeout": 30,
            "concurrent_fragment_downloads": int(cfg.get("fragments", 16)),
            "http_chunk_size": int(cfg.get("chunk", 10485760)),
            "geo_bypass": True,
            "nocheckcertificate": bool(cfg.get("insecure")),
            "http_headers": {"User-Agent": UA},
            "logger": _Logger(job),
            "progress_hooks": [self._hook(job)],
            "postprocessor_hooks": [self._pp_hook(job)],
            "noplaylist": not bool(cfg.get("playlist")),
            "download_archive": str(incoming.parent / storage.ARCHIVE),
            "ignoreerrors": False,
        }
        if cfg.get("proxy"):
            opts["proxy"] = cfg["proxy"]
        if cfg.get("rate"):
            opts["ratelimit"] = int(float(cfg["rate"]) * 1024 * 1024)
        if cfg.get("cookies"):
            opts["cookiefile"] = cfg["cookies"]

        quality = cfg.get("quality", "best")
        if cfg.get("audio_only"):
            opts["format"] = "ba/b"
        elif job.fmt:
            opts["format"] = job.fmt
        elif quality == "best":
            opts["format"] = "bv*+ba/b"
        else:
            opts["format"] = f"bv*[height<={quality}]+ba/b[height<={quality}]/b"
        opts["merge_output_format"] = "mp4"

        sort = []
        if quality != "best":
            sort.append(f"res:{quality}")
        if cfg.get("compat"):
            sort += ["vcodec:h264", "acodec:aac", "ext:mp4"]
        if sort:
            opts["format_sort"] = sort

        pps: list[dict] = []
        if cfg.get("audio_only"):
            pps.append({"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"})
        if cfg.get("subs"):
            opts.update(
                {
                    "writesubtitles": True,
                    "writeautomaticsub": True,
                    "subtitleslangs": ["id", "en"],
                    "subtitlesformat": "srt/best",
                }
            )
        if cfg.get("thumb"):
            opts["writethumbnail"] = True
            pps.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})
        if pps:
            opts["postprocessors"] = pps
        return opts

    def _hook(self, job: Job):
        def hook(d: dict) -> None:
            if job.cancel.is_set():
                raise Cancelled()
            status = d.get("status")
            if status == "downloading":
                job.status = DOWNLOADING
                job.done = int(d.get("downloaded_bytes") or 0)
                job.total = int(d.get("total_bytes") or d.get("total_bytes_estimate") or 0)
                job.speed = float(d.get("speed") or 0.0)
                job.eta = d.get("eta")
            elif status == "finished":
                job.status = PROCESSING
                job.done = job.total or job.done

        return hook

    def _pp_hook(self, job: Job):
        def hook(d: dict) -> None:
            if job.cancel.is_set():
                raise Cancelled()
            if d.get("status") == "started":
                job.status = PROCESSING
                job.log.append(f"postproses: {d.get('postprocessor')}")

        return hook

    def _run(self, job: Job) -> None:
        root = Path(self.cfg["dl_dir"]).expanduser()
        incoming = root / storage.INCOMING
        root.mkdir(parents=True, exist_ok=True)
        incoming.mkdir(parents=True, exist_ok=True)
        job.site = storage.site_of(job.url)
        job.status = PROBING

        probe_opts = {
            "quiet": True,
            "noprogress": True,
            "no_warnings": True,
            "skip_download": True,
            "socket_timeout": 30,
            "noplaylist": True,
            "http_headers": {"User-Agent": UA},
            "logger": _Logger(job),
            "nocheckcertificate": bool(self.cfg.get("insecure")),
        }
        if self.cfg.get("proxy"):
            probe_opts["proxy"] = self.cfg["proxy"]
        if self.cfg.get("cookies"):
            probe_opts["cookiefile"] = self.cfg["cookies"]

        with yt_dlp.YoutubeDL(probe_opts) as ydl:
            info = ydl.extract_info(job.url, download=False) or {}
        if info.get("_type") == "playlist":
            entries = [e for e in (info.get("entries") or []) if e]
            info = entries[0] if entries else info
        job.title = info.get("title") or job.url
        job.vid = str(info.get("id") or "")
        job.duration = info.get("duration")

        existing = storage.find_existing(root, job.vid)
        if existing:
            job.filepath = str(existing)
            job.status = EXISTS
            job.log.append(f"sudah ada: {existing.name}")
            return

        needed = 0
        for f in info.get("formats") or []:
            size = f.get("filesize") or f.get("filesize_approx")
            if size:
                needed = max(needed, int(size))
        margin = int(float(self.cfg.get("margin_mb", 500)) * 1024 * 1024)
        if storage.free_bytes(root) < (needed or 0) + margin:
            job.status = NO_SPACE
            job.hint = f"Perlu ±{storage.human(needed)} + margin {storage.human(margin)}."
            return

        quota = float(self.cfg.get("quota_gb", 0) or 0) * 1024**3
        if quota and self.session_bytes >= quota:
            job.status = QUEUED
            job.hint = "Kuota sesi tercapai."
            self._q.put(job.id)
            return

        if job.cancel.is_set():
            job.status = CANCELLED
            return

        started = time.time() - 1
        job.status = DOWNLOADING
        with yt_dlp.YoutubeDL(self._opts(job, incoming)) as ydl:
            ydl.download([job.url])

        produced = storage.collect_new(incoming, started)
        if not produced:
            job.status = ERROR
            job.error = "Tidak ada file yang dihasilkan."
            job.category, job.hint = classify(job.log[-1] if job.log else "")
            return

        moved = storage.move_in(produced, root)
        if not moved:
            job.status = ERROR
            job.error = "File gagal dipindahkan dari folder sementara."
            return
        job.filepath = str(moved[0])
        job.status = DONE
        try:
            self.session_bytes += sum(p.stat().st_size for p in moved)
        except Exception:
            pass
