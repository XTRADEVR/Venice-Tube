#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Venice Tube Grabber
Streamlit downloader berbasis yt-dlp dengan DNS paksa Cloudflare 1.1.1.1
(UDP / DNS-over-HTTPS) dan target penyimpanan ke kartu memori HP.

Jalankan:
    streamlit run app.py --server.address 0.0.0.0 --server.port 8501
"""

from __future__ import annotations

import os
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path

import dns.message
import dns.rdatatype
import dns.resolver
import requests
import streamlit as st
import yt_dlp

try:  # opsional: impersonasi TLS ala browser
    from yt_dlp.networking.impersonate import ImpersonateTarget

    HAS_IMPERSONATE = True
except Exception:
    ImpersonateTarget = None
    HAS_IMPERSONATE = False

APP_TITLE = "Venice Tube Grabber"
CF_V4 = ["1.1.1.1", "1.0.0.1"]
CF_DOH = "https://cloudflare-dns.com/dns-query"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
FFMPEG = shutil.which("ffmpeg")
ARIA2C = shutil.which("aria2c")


# --------------------------------------------------------------------------- #
# DNS router: semua resolusi dipaksa lewat Cloudflare 1.1.1.1
# --------------------------------------------------------------------------- #
class DNSRouter:
    def __init__(self) -> None:
        self._orig_getaddrinfo = socket.getaddrinfo
        self._orig_gethostbyname = socket.gethostbyname
        self._cache: dict[tuple, tuple[float, list[str]]] = {}
        self._lock = threading.Lock()
        self._local = threading.local()
        self.mode = "system"          # system | udp | doh
        self.ipv4_only = True
        self.ttl = 300.0
        self._resolver = dns.resolver.Resolver(configure=False)
        self._resolver.nameservers = CF_V4
        self._resolver.timeout = 4.0
        self._resolver.lifetime = 6.0

    # ---------------------------- query ---------------------------- #
    def _doh(self, name: str, rdtype: int) -> list[str]:
        query = dns.message.make_query(name, rdtype, use_edns=False)
        resp = requests.post(
            CF_DOH,
            data=query.to_wire(),
            headers={
                "Content-Type": "application/dns-message",
                "Accept": "application/dns-message",
                "User-Agent": UA,
            },
            timeout=8,
        )
        resp.raise_for_status()
        msg = dns.message.from_wire(resp.content)
        out: list[str] = []
        for rrset in msg.answer:
            if rrset.rdtype != rdtype:
                continue
            for rr in rrset:
                out.append(rr.address)
        return out

    def _udp(self, name: str, rdtype: int) -> list[str]:
        ans = self._resolver.resolve(name, rdtype, raise_on_no_answer=False)
        if ans.rrset is None:
            return []
        return [r.address for r in ans]

    def resolve(self, host: str, want4: bool, want6: bool) -> list[str]:
        key = (host, int(want4), int(want6), self.mode)
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return list(hit[1])

        ips: list[str] = []
        tasks = []
        if want4:
            tasks.append(dns.rdatatype.A)
        if want6:
            tasks.append(dns.rdatatype.AAAA)
        for rdtype in tasks:
            try:
                if self.mode == "doh":
                    ips.extend(self._doh(host, rdtype))
                else:
                    ips.extend(self._udp(host, rdtype))
            except Exception:
                continue

        with self._lock:
            self._cache[key] = (now + self.ttl, list(ips))
        return ips

    # ------------------------ patched socket ----------------------- #
    def _patched_getaddrinfo(self, host, port, family=0, type=0, proto=0, flags=0):
        orig = self._orig_getaddrinfo
        if (
            self.mode == "system"
            or host is None
            or isinstance(host, bytes)
            or getattr(self._local, "busy", False)
        ):
            return orig(host, port, family, type, proto, flags)

        host = str(host)
        for fam, probe in (
            (socket.AF_INET, socket.inet_pton),
            (socket.AF_INET6, socket.inet_pton),
        ):
            try:
                probe(fam, host)
                return orig(host, port, family, type, proto, flags)
            except OSError:
                pass

        want6 = family == socket.AF_INET6 or (family == 0 and not self.ipv4_only)
        want4 = family in (0, socket.AF_INET)

        self._local.busy = True
        try:
            ips = self.resolve(host, want4, want6)
        finally:
            self._local.busy = False

        if not ips:
            return orig(host, port, family, type, proto, flags)

        socktype = type or socket.SOCK_STREAM
        results = []
        for ip in ips:
            if ":" in ip:
                if family == socket.AF_INET:
                    continue
                fam = socket.AF_INET6
                sockaddr = (ip, port or 0, 0, 0)
            else:
                if family == socket.AF_INET6:
                    continue
                fam = socket.AF_INET
                sockaddr = (ip, port or 0)
            results.append((fam, socktype, proto or 0, "", sockaddr))

        if not results:
            return orig(host, port, family, type, proto, flags)
        return results

    def _patched_gethostbyname(self, host):
        if self.mode == "system":
            return self._orig_gethostbyname(host)
        try:
            infos = self._patched_getaddrinfo(
                host, 0, socket.AF_INET, socket.SOCK_STREAM
            )
        except Exception:
            return self._orig_gethostbyname(host)
        for fam, _, _, _, sockaddr in infos:
            if fam == socket.AF_INET:
                return sockaddr[0]
        return self._orig_gethostbyname(host)

    def install(self) -> None:
        socket.getaddrinfo = self._patched_getaddrinfo
        socket.gethostbyname = self._patched_gethostbyname


@st.cache_resource(show_spinner=False)
def dns_router() -> DNSRouter:
    router = DNSRouter()
    router.install()
    return router


# --------------------------------------------------------------------------- #
# Util
# --------------------------------------------------------------------------- #
def human(num) -> str:
    try:
        num = float(num)
    except (TypeError, ValueError):
        return "?"
    if num <= 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


def human_time(sec) -> str:
    try:
        sec = int(sec)
    except (TypeError, ValueError):
        return "--:--"
    if sec < 0:
        return "--:--"
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def default_dir() -> str:
    env = os.environ.get("DL_DIR")
    if env:
        return env
    for cand in (
        "/storage/emulated/0/Download/VeniceGrab",
        "/sdcard/Download/VeniceGrab",
        str(Path.home() / "Downloads" / "VeniceGrab"),
    ):
        if Path(cand).parent.exists():
            return cand
    return str(Path.cwd() / "downloads")


class YTLogger:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def debug(self, msg: str) -> None:
        if msg.startswith("[debug]"):
            return
        self.lines.append(msg)

    def info(self, msg: str) -> None:
        self.lines.append(msg)

    def warning(self, msg: str) -> None:
        self.lines.append(f"WARN: {msg}")

    def error(self, msg: str) -> None:
        self.lines.append(f"ERROR: {msg}")


class UI:
    """Progress bar + detail, di-throttle supaya UI tidak banjir update."""

    def __init__(self) -> None:
        self.bar = st.progress(0.0, text="Menyiapkan...")
        self.detail = st.empty()
        self._last = 0.0

    def reset(self) -> None:
        self.bar.progress(0.0, text="Menyiapkan...")
        self.detail.empty()

    def hook(self, d: dict) -> None:
        status = d.get("status")
        now = time.monotonic()
        if status == "downloading":
            if now - self._last < 0.35:
                return
            self._last = now
            done = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            frac = (done / total) if total else 0.0
            self.bar.progress(
                min(max(frac, 0.0), 1.0), text=f"Mengunduh {frac * 100:.1f}%"
            )
            self.detail.markdown(
                f"**{human(done)}** / {human(total) if total else '?'} · "
                f"{human(d.get('speed'))}/s · sisa {human_time(d.get('eta'))}"
            )
        elif status == "finished":
            self.bar.progress(1.0, text="Selesai unduh, memproses file...")
            self.detail.markdown(f"`{Path(d.get('filename', '')).name}`")
        elif status == "error":
            self.bar.progress(0.0, text="Gagal")


def build_opts(cfg: dict) -> dict:
    """Susun opsi yt-dlp dari konfigurasi UI."""
    opts: dict = {
        "paths": {"home": str(cfg["dl_dir"])},
        "outtmpl": {"default": "%(title).150B [%(id)s].%(ext)s"},
        "windowsfilenames": True,
        "trim_file_name": 120,
        "overwrites": False,
        "continuedl": True,
        "nopart": False,
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "retries": 20,
        "fragment_retries": 20,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": cfg["fragments"],
        "http_chunk_size": cfg["chunk"],
        "geo_bypass": True,
        "nocheckcertificate": cfg["insecure"],
        "http_headers": {"User-Agent": UA},
        "logger": cfg["logger"],
        "progress_hooks": [cfg["hook"]],
        "postprocessor_hooks": [cfg["pp_hook"]],
        "noplaylist": not cfg["playlist"],
    }

    if cfg["format"]:
        opts["format"] = cfg["format"]
    else:
        opts["format"] = "bv*+ba/b"
    opts["merge_output_format"] = "mp4"

    if cfg["proxy"]:
        opts["proxy"] = cfg["proxy"]
    if cfg["rate"]:
        opts["ratelimit"] = int(cfg["rate"] * 1024 * 1024)
    if cfg["cookies"]:
        opts["cookiefile"] = cfg["cookies"]

    postprocessors: list[dict] = []
    if cfg["audio_only"]:
        opts["format"] = "ba/b"
        postprocessors.append(
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        )
    if cfg["subs"]:
        opts.update(
            {
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": ["id", "en"],
                "subtitlesformat": "srt/best",
            }
        )
    if cfg["thumb"]:
        opts["writethumbnail"] = True
        postprocessors.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})
    if postprocessors:
        opts["postprocessors"] = postprocessors

    if cfg["impersonate"] and HAS_IMPERSONATE:
        try:
            opts["impersonate"] = ImpersonateTarget("chrome")
        except Exception:
            pass

    if cfg["aria2c"] and ARIA2C:
        opts["external_downloader"] = {"default": ARIA2C}
        opts["external_downloader_args"] = {
            "aria2c": ["-x", "16", "-s", "16", "-k", "1M", "--file-allocation=none"]
        }

    return opts


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
def main() -> None:
    st.set_page_config(page_title=APP_TITLE, page_icon="🎬", layout="wide")
    router = dns_router()

    st.session_state.setdefault("dl_dir", default_dir())
    st.session_state.setdefault("info", None)
    st.session_state.setdefault("info_url", "")

    # ------------------------------ sidebar ------------------------------ #
    with st.sidebar:
        st.header("Jaringan")
        mode_label = st.radio(
            "Resolver DNS",
            ["Cloudflare 1.1.1.1 (UDP)", "Cloudflare DoH", "Sistem"],
            index=0,
            help="UDP 1.1.1.1 = resolusi paling cepat. DoH = terenkripsi.",
        )
        router.mode = {
            "Cloudflare 1.1.1.1 (UDP)": "udp",
            "Cloudflare DoH": "doh",
            "Sistem": "system",
        }[mode_label]
        router.ipv4_only = st.checkbox("IPv4 saja (paling stabil)", value=True)
        st.caption("Semua koneksi app dipaksa lewat resolver terpilih.")

        st.divider()
        st.header("Penyimpanan")
        c1, c2, c3 = st.columns(3)
        if c1.button("Kartu SD", use_container_width=True):
            st.session_state["dl_dir"] = "/storage/emulated/0/Download/VeniceGrab"
        if c2.button("Movies", use_container_width=True):
            st.session_state["dl_dir"] = "/storage/emulated/0/Movies/VeniceGrab"
        if c3.button("Lokal", use_container_width=True):
            st.session_state["dl_dir"] = str(Path.cwd() / "downloads")
        st.text_input("Folder tujuan", key="dl_dir")
        st.caption("Termux/Android: jalankan `termux-setup-storage` dulu.")

        st.divider()
        st.header("Kecepatan")
        fragments = st.slider("Fragmen paralel", 1, 32, 16)
        chunk = st.select_slider(
            "Ukuran chunk HTTP",
            options=[1048576, 5242880, 10485760, 20971520],
            value=10485760,
            format_func=human,
        )
        rate = st.number_input(
            "Batas kecepatan (MB/s, 0 = tanpa batas)", min_value=0.0, value=0.0, step=1.0
        )
        use_aria2c = st.checkbox(
            f"aria2c multi-koneksi ({'ada' if ARIA2C else 'tidak terpasang'})",
            value=bool(ARIA2C),
            disabled=not ARIA2C,
        )
        if use_aria2c and ARIA2C:
            st.caption("aria2c: progress per-file tidak ditampilkan.")

        st.divider()
        st.header("Lainnya")
        proxy = st.text_input("Proxy (opsional)", placeholder="http://ip:port")
        insecure = st.checkbox("Abaikan sertifikat TLS", value=False)
        use_impersonate = st.checkbox(
            "Samarkan TLS seperti Chrome",
            value=HAS_IMPERSONATE,
            disabled=not HAS_IMPERSONATE,
            help="Bantu untuk situs yang memblokir bot.",
        )
        cookie_file = st.file_uploader("cookies.txt (Netscape)", type=["txt"])

        if FFMPEG:
            st.success("ffmpeg siap (merge & konversi MP3).")
        else:
            st.warning("ffmpeg tidak ada: merge/MP3/thumbnail nonaktif.")

    dl_dir = Path(st.session_state["dl_dir"]).expanduser()
    try:
        dl_dir.mkdir(parents=True, exist_ok=True)
        test = dl_dir / ".venice_write_test"
        test.write_bytes(b"1")
        test.unlink()
        dir_ok = True
    except Exception as exc:
        dir_ok = False
        st.sidebar.error(f"Folder tidak siap: {exc}")

    if dir_ok:
        usage = shutil.disk_usage(dl_dir)
        st.sidebar.caption(f"Kosong: **{human(usage.free)}** / {human(usage.total)}")

    cookies_path = None
    if cookie_file is not None:
        cookies_path = Path(tempfile.gettempdir()) / "venice_cookies.txt"
        cookies_path.write_bytes(cookie_file.getvalue())

    # ------------------------------- main -------------------------------- #
    st.title("🎬 Venice Tube Grabber")
    st.caption(
        f"Target: `{dl_dir}` · DNS: **{mode_label}** · "
        f"Fragmen: **{fragments}** · ffmpeg: **{'ya' if FFMPEG else 'tidak'}**"
    )

    url_text = st.text_area(
        "Tempel satu atau beberapa link (satu per baris)",
        height=140,
        placeholder="https://www.xvideos.com/video...\nhttps://www.pornhub.com/view_video.php?viewkey=...",
    )

    col_fetch, col_opts = st.columns([1, 3])
    with col_fetch:
        fetch = st.button("Ambil info", use_container_width=True)
    with col_opts:
        audio_only = st.checkbox("Audio saja (MP3)", value=False)
        subs = st.checkbox("Unduh subtitle", value=False)
        thumb = st.checkbox("Sematkan thumbnail", value=False)
        playlist = st.checkbox("Ikuti playlist/semua bagian", value=False)

    if fetch:
        urls = [u.strip() for u in url_text.splitlines() if u.strip()]
        if not urls:
            st.warning("Isi minimal satu link.")
        else:
            st.session_state["info"] = None
            st.session_state["info_url"] = urls[0]
            logger = YTLogger()
            probe_opts = {
                "quiet": True,
                "noprogress": True,
                "no_warnings": True,
                "skip_download": True,
                "socket_timeout": 30,
                "noplaylist": True,
                "http_headers": {"User-Agent": UA},
                "logger": logger,
                "geo_bypass": True,
            }
            if proxy:
                probe_opts["proxy"] = proxy
            if cookies_path:
                probe_opts["cookiefile"] = str(cookies_path)
            if use_impersonate and HAS_IMPERSONATE:
                try:
                    probe_opts["impersonate"] = ImpersonateTarget("chrome")
                except Exception:
                    pass
            try:
                with st.spinner("Mengambil metadata..."):
                    with yt_dlp.YoutubeDL(probe_opts) as ydl:
                        st.session_state["info"] = ydl.extract_info(urls[0], download=False)
            except Exception as exc:
                st.error(f"Gagal ambil info: {exc}")
                if logger.lines:
                    st.code("\n".join(logger.lines[-40:]))

    info = st.session_state.get("info")
    chosen_format = None
    if info:
        if info.get("_type") == "playlist":
            entries = info.get("entries") or []
            st.info(f"Playlist terdeteksi: {len(entries)} item — aktifkan opsi playlist.")
        st.markdown(
            f"**{info.get('title', '(tanpa judul)')}**  \n"
            f"Durasi {human_time(info.get('duration'))} · "
            f"{info.get('uploader') or info.get('channel') or '-'} · "
            f"{info.get('extractor_key')}"
        )
        if info.get("thumbnail"):
            st.image(info["thumbnail"], width=320)

        rows = []
        for f in info.get("formats") or []:
            if f.get("vcodec") == "none" and f.get("acodec") == "none":
                continue
            rows.append(
                {
                    "id": f.get("format_id"),
                    "resolusi": f.get("resolution") or f.get("format_note") or "-",
                    "ext": f.get("ext"),
                    "vcodec": (f.get("vcodec") or "-")[:16],
                    "acodec": (f.get("acodec") or "-")[:16],
                    "ukuran": human(f.get("filesize") or f.get("filesize_approx")),
                    "tbr": f.get("tbr") or 0,
                }
            )
        rows.sort(key=lambda r: r["tbr"], reverse=True)
        if rows:
            st.dataframe(rows, use_container_width=True, hide_index=True)
            labels = ["auto (terbaik)"] + [
                f"{r['id']} · {r['resolusi']} · {r['ext']} · {r['ukuran']}" for r in rows
            ]
            pick = st.selectbox("Format", labels, index=0)
            if pick != labels[0]:
                chosen_format = pick.split(" · ")[0]
    else:
        st.session_state.setdefault("_noop", None)

    start = st.button(
        "⬇️ Mulai unduh",
        type="primary",
        use_container_width=True,
        disabled=not dir_ok,
    )

    if start:
        urls = [u.strip() for u in url_text.splitlines() if u.strip()]
        if not urls:
            st.warning("Isi minimal satu link.")
        elif not FFMPEG and (audio_only or thumb):
            st.error("ffmpeg belum terpasang, matikan opsi audio/thumbnail.")
        else:
            ui = UI()
            results: list[tuple[str, bool, str]] = []
            for idx, url in enumerate(urls, start=1):
                st.markdown(f"**({idx}/{len(urls)})** `{url}`")
                ui.reset()
                logger = YTLogger()
                cfg = {
                    "dl_dir": dl_dir,
                    "fragments": fragments,
                    "chunk": chunk,
                    "rate": rate,
                    "proxy": proxy,
                    "insecure": insecure,
                    "cookies": str(cookies_path) if cookies_path else None,
                    "impersonate": use_impersonate,
                    "aria2c": use_aria2c,
                    "playlist": playlist,
                    "audio_only": audio_only,
                    "subs": subs,
                    "thumb": thumb,
                    "format": chosen_format,
                    "logger": logger,
                    "hook": ui.hook,
                    "pp_hook": ui.hook,
                }
                try:
                    with yt_dlp.YoutubeDL(build_opts(cfg)) as ydl:
                        ydl.download([url])
                    results.append((url, True, ""))
                    ui.bar.progress(1.0, text="Selesai")
                except Exception as exc:
                    results.append((url, False, str(exc)))
                    ui.bar.progress(0.0, text="Gagal")
                with st.expander(
                    f"Log #{idx}", expanded=not results[-1][1]
                ):
                    st.code("\n".join(logger.lines[-200:]) or "(tidak ada output)")

            ok = sum(1 for _, good, _ in results if good)
            st.success(f"{ok}/{len(results)} link berhasil diunduh.")
            for url, good, err in results:
                if not good:
                    st.error(f"{url}\n\n{err}")

    st.divider()
    st.subheader("File di folder tujuan")
    if dir_ok:
        files = sorted(
            (
                p
                for p in dl_dir.iterdir()
                if p.is_file() and not p.name.endswith((".part", ".ytdl", ".tmp"))
            ),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:30]
        if not files:
            st.caption("Belum ada file.")
        for path in files:
            size = path.stat().st_size
            left, right = st.columns([4, 1])
            left.markdown(f"`{path.name}`  \n{human(size)}")
            with open(path, "rb") as fh:
                right.download_button(
                    "Simpan ke HP",
                    data=fh,
                    file_name=path.name,
                    mime="application/octet-stream",
                    key=f"dl_{path.name}_{size}",
                    use_container_width=True,
                )
    else:
        st.caption("Folder tujuan belum valid.")


if __name__ == "__main__":
    main()
