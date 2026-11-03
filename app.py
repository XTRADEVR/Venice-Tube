#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Venice Tube Grabber — Streamlit + yt-dlp, DNS 1.1.1.1, antrean worker."""
from __future__ import annotations

import hashlib
import hmac
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import requests
import streamlit as st

import config
import dns_router
import storage
from downloader import JobManager

APP = "Venice Tube Grabber"
FFMPEG = shutil.which("ffmpeg")
ARIA2C = shutil.which("aria2c")

st.set_page_config(page_title=APP, page_icon="🎬", layout="centered")


def boot() -> None:
    if "cfg" not in st.session_state:
        st.session_state["cfg"] = config.load()
    dns_router.ROUTER.install()
    dns_router.ROUTER.mode = st.session_state["cfg"].get("dns_mode", "udp")
    dns_router.ROUTER.ipv4_only = st.session_state["cfg"].get("ipv4_only", True)


def login_gate() -> bool:
    expected = (os.environ.get("APP_PASSWORD") or "").strip()
    if not expected:
        return True
    if st.session_state.get("authed"):
        return True
    st.title("🎬 " + APP)
    with st.form("login"):
        pwd = st.text_input("Password", type="password")
        ok = st.form_submit_button("Masuk", use_container_width=True)
    if ok:
        if hmac.compare_digest(
            hashlib.sha256(pwd.encode()).hexdigest(),
            hashlib.sha256(expected.encode()).hexdigest(),
        ):
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.session_state["fails"] = st.session_state.get("fails", 0) + 1
            st.error(f"Password salah ({st.session_state['fails']}x).")
    return False


def persist(cfg: dict) -> None:
    if cfg != st.session_state.get("_saved"):
        config.save(cfg)
        st.session_state["_saved"] = dict(cfg)


def manager(cfg: dict) -> JobManager:
    mgr = st.session_state.get("mgr")
    if mgr is None:
        mgr = JobManager(cfg)
        st.session_state["mgr"] = mgr
    else:
        mgr.update_cfg(cfg)
    return mgr


def latest_ytdlp() -> str:
    try:
        r = requests.get("https://pypi.org/pypi/yt-dlp/json", timeout=10)
        return r.json()["info"]["version"]
    except Exception:
        return "?"


def main() -> None:
    boot()
    if not login_gate():
        return

    cfg = st.session_state["cfg"]
    mgr = manager(cfg)

    with st.sidebar:
        st.header("Jaringan")
        mode = st.radio(
            "Resolver",
            ["udp", "doh", "system"],
            index=["udp", "doh", "system"].index(cfg.get("dns_mode", "udp")),
            format_func=lambda m: {
                "udp": "Cloudflare 1.1.1.1 (UDP)",
                "doh": "Cloudflare DoH",
                "system": "Sistem",
            }[m],
        )
        cfg["dns_mode"] = mode
        cfg["ipv4_only"] = st.checkbox("IPv4 saja", value=cfg.get("ipv4_only", True))
        dns_router.ROUTER.mode = mode
        dns_router.ROUTER.ipv4_only = cfg["ipv4_only"]
        if st.button("Uji resolver", use_container_width=True):
            ok, msg = dns_router.ROUTER.probe()
            (st.success if ok else st.error)(msg)

        st.divider()
        st.header("Penyimpanan")
        cfg["dl_dir"] = st.text_input("Folder", value=cfg["dl_dir"])
        c1, c2 = st.columns(2)
        if c1.button("Kartu SD", use_container_width=True):
            cfg["dl_dir"] = "/storage/emulated/0/Download/VeniceGrab"
            st.rerun()
        if c2.button("Movies", use_container_width=True):
            cfg["dl_dir"] = "/storage/emulated/0/Movies/VeniceGrab"
            st.rerun()
        cfg["margin_mb"] = st.number_input("Margin aman (MB)", 50, 5000, int(cfg["margin_mb"]), 50)
        cfg["quota_gb"] = st.number_input("Kuota sesi (GB, 0 = bebas)", 0.0, 500.0, float(cfg["quota_gb"]), 1.0)

        st.divider()
        st.header("Kecepatan & format")
        cfg["parallel"] = st.slider("Unduhan paralel", 1, 4, int(cfg["parallel"]))
        cfg["fragments"] = st.slider("Fragmen paralel", 1, 32, int(cfg["fragments"]))
        cfg["chunk"] = st.select_slider(
            "Chunk HTTP", [1048576, 5242880, 10485760, 20971520], value=int(cfg["chunk"]), format_func=storage.human
        )
        cfg["rate"] = st.number_input("Batas MB/s (0 = bebas)", 0.0, 100.0, float(cfg["rate"]), 1.0)
        cfg["quality"] = st.selectbox(
            "Batas resolusi",
            ["best", "1080", "720", "480", "360"],
            index=["best", "1080", "720", "480", "360"].index(cfg["quality"]),
        )
        cfg["compat"] = st.checkbox("Prioritaskan H.264/MP4 (kompatibel HP)", value=cfg["compat"])
        cfg["audio_only"] = st.checkbox("Audio MP3 saja", value=cfg["audio_only"])
        cfg["subs"] = st.checkbox("Subtitle", value=cfg["subs"])
        cfg["thumb"] = st.checkbox("Sematkan thumbnail", value=cfg["thumb"])
        cfg["playlist"] = st.checkbox("Ikuti playlist", value=cfg["playlist"])
        cfg["restrict"] = st.checkbox("Nama file ASCII aman", value=cfg["restrict"])

        st.divider()
        st.header("Lainnya")
        cfg["proxy"] = st.text_input("Proxy", value=cfg["proxy"])
        cfg["insecure"] = st.checkbox("Abaikan sertifikat TLS", value=cfg["insecure"])
        cookie = st.file_uploader("cookies.txt", type=["txt"])
        if cookie is not None:
            path = config.BASE / ".config" / "cookies.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(cookie.getvalue())
            try:
                path.chmod(0o600)
            except Exception:
                pass
            cfg["cookies"] = str(path)
        if cfg.get("cookies"):
            st.caption(f"Cookies aktif: `{Path(cfg['cookies']).name}`")
            if st.button("Hapus cookies", use_container_width=True):
                try:
                    Path(cfg["cookies"]).unlink()
                except Exception:
                    pass
                cfg.pop("cookies", None)
                st.rerun()

        st.divider()
        st.caption(f"ffmpeg: {'ada' if FFMPEG else 'TIDAK ADA'} · aria2c: {'ada' if ARIA2C else '-'}")

    persist(cfg)

    root = Path(cfg["dl_dir"]).expanduser()
    ok_dir, dir_err = storage.prepare(root)

    st.title("🎬 " + APP)
    if ok_dir:
        st.caption(f"`{root}` · kosong {storage.human(storage.free_bytes(root))} · DNS {mode} · paralel {cfg['parallel']}")
    else:
        st.error(f"Folder tidak siap: {dir_err}")

    # ------------------------------ tambah ------------------------------ #
    with st.form("add", clear_on_submit=True):
        url = st.text_input("Link", placeholder="https://...")
        c1, c2 = st.columns([1, 3])
        probe = c1.form_submit_button("Cek format", use_container_width=True)
        add = c2.form_submit_button("Tambah ke antrean", type="primary", use_container_width=True)
        if add and url:
            good, msg = mgr.add(url)
            (st.success if good else st.warning)("Masuk antrean." if good else msg)
        if probe and url:
            st.session_state["probe_url"] = url

    bulk = st.text_area("Tambah massal (satu per baris)", height=90)
    if st.button("Masukkan semua", disabled=not bulk.strip()):
        added = 0
        for line in bulk.splitlines():
            if line.strip():
                good, _ = mgr.add(line.strip())
                added += 1 if good else 0
        st.success(f"{added} link masuk antrean.")

    probe_url = st.session_state.get("probe_url")
    if probe_url:
        try:
            import yt_dlp

            with st.spinner("Mengambil metadata..."):
                with yt_dlp.YoutubeDL(
                    {"quiet": True, "no_warnings": True, "noprogress": True,
                     "skip_download": True, "noplaylist": True}
                ) as ydl:
                    info = ydl.extract_info(probe_url, download=False) or {}
            st.markdown(f"**{info.get('title','(tanpa judul)')}** · {storage.human_time(info.get('duration'))}")
            rows = [
                {"id": f.get("format_id"), "res": f.get("resolution") or "-",
                 "ext": f.get("ext"), "size": storage.human(f.get("filesize") or f.get("filesize_approx"))}
                for f in (info.get("formats") or [])
                if f.get("vcodec") != "none" or f.get("acodec") != "none"
            ]
            pick = st.selectbox("Format", ["auto"] + [f"{r['id']} · {r['res']} · {r['ext']} · {r['size']}" for r in rows])
            if st.button("Tambah dengan format ini", type="primary"):
                fmt = None if pick == "auto" else pick.split(" · ")[0]
                good, msg = mgr.add(probe_url, fmt)
                (st.success if good else st.warning)("Masuk antrean." if good else msg)
        except Exception as exc:
            st.error(f"Gagal probe: {exc}")

    # ------------------------------ antrean ----------------------------- #
    st.divider()
    st.subheader("Antrean")

    @st.fragment(run_every="1.2s")
    def queue_view() -> None:
        stats = mgr.stats()
        st.caption(
            f"antre {stats['queued']} · aktif {stats['active']} · "
            f"selesai {stats['done']} · gagal {stats['failed']} · "
            f"terunduh {storage.human(stats['session_bytes'])}"
        )
        jobs = mgr.snapshot()
        if not jobs:
            st.caption("Antrean kosong.")
        for job in jobs:
            icon = {
                "done": "✅", "error": "⛔", "cancelled": "⏹", "exists": "♻️",
                "no_space": "💾", "paused": "⏸", "queued": "🕒",
            }.get(job["status"], "⬇️")
            with st.container(border=True):
                st.markdown(f"{icon} **{job['title'][:110]}**  \n`{job['url'][:90]}`")
                if job["status"] == "downloading":
                    total = job["total"] or 0
                    frac = (job["done"] / total) if total else 0.0
                    st.progress(min(max(frac, 0.0), 1.0))
                    st.caption(
                        f"{storage.human(job['done'])} / {storage.human(total) if total else '?'} · "
                        f"{storage.human(job['speed'])}/s · sisa {storage.human_time(job['eta'])}"
                    )
                elif job["status"] == "processing":
                    st.info("Memproses file (merge/konversi)...")
                elif job["status"] == "error":
                    st.error(job["error"])
                    if job["hint"]:
                        st.caption("Saran: " + job["hint"])
                elif job["status"] == "exists":
                    st.caption(f"Sudah ada: {Path(job['filepath']).name if job['filepath'] else '-'}")
                elif job["status"] == "no_space":
                    st.warning("Ruang tidak cukup. " + (job["hint"] or ""))

                cols = st.columns(5)
                if job["status"] in {"queued", "downloading", "processing", "probing"}:
                    if cols[0].button("Batal", key=f"c{job['id']}", use_container_width=True):
                        mgr.cancel(job["id"])
                if job["status"] in {"error", "cancelled", "paused", "no_space"}:
                    if cols[1].button("Ulangi", key=f"r{job['id']}", use_container_width=True):
                        mgr.resume(job["id"])
                if job["status"] == "downloading":
                    if cols[2].button("Jeda", key=f"p{job['id']}", use_container_width=True):
                        mgr.pause(job["id"])
                if job["status"] in {"error", "cancelled", "done", "exists", "no_space"}:
                    if cols[3].button("Hapus", key=f"x{job['id']}", use_container_width=True):
                        mgr.remove(job["id"])
                with cols[4].popover("Log", use_container_width=True):
                    st.code("\n".join(job["log"]) or "(kosong)")

        a, b = st.columns(2)
        if a.button("Ulangi semua gagal", use_container_width=True):
            st.toast(f"{mgr.retry_failed()} job diulang")
        if b.button("Bersihkan selesai", use_container_width=True):
            mgr.clear_finished()

    queue_view()

    # ----------------------------- file manager -------------------------- #
    st.divider()
    st.subheader("File")
    if ok_dir:
        sites = sorted({p.name.split(" - ")[0] for p in storage.list_media(root)}) if root.exists() else []
        f1, f2 = st.columns(2)
        site = f1.selectbox("Filter situs", ["semua"] + sites)
        sort = f2.selectbox("Urutkan", ["terbaru", "terlama", "terbesar", "nama"])
        files = storage.list_media(root, "" if site == "semua" else site, sort)[:80]
        if not files:
            st.caption("Belum ada file.")
        for p in files:
            try:
                size = p.stat().st_size
            except Exception:
                continue
            with st.container(border=True):
                st.markdown(f"`{p.name}`  \n{storage.human(size)}")
                c1, c2, c3 = st.columns(3)
                with open(p, "rb") as fh:
                    c1.download_button(
                        "Simpan ke HP", data=fh, file_name=p.name,
                        mime="application/octet-stream", key=f"d{p.name}{size}", use_container_width=True,
                    )
                with c2.popover("Pratinjau", use_container_width=True):
                    if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}:
                        st.video(str(p))
                    elif p.suffix.lower() in {".jpg", ".png", ".webp"}:
                        st.image(str(p))
                    else:
                        st.caption("Tidak ada pratinjau.")
                if c3.button("Hapus", key=f"del{p.name}{size}", use_container_width=True):
                    try:
                        p.unlink()
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))

        with st.expander("Aksi massal"):
            names = [p.name for p in files]
            chosen = st.multiselect("Pilih file", names)
            target = st.text_input("Folder tujuan pindah", value="/storage/emulated/0/Movies/VeniceGrab")
            b1, b2 = st.columns(2)
            if b1.button("Pindahkan terpilih", use_container_width=True) and chosen:
                moved = storage.move_in([root / n for n in chosen if (root / n).exists()], Path(target).expanduser())
                st.success(f"{len(moved)} file dipindahkan.")
            if b2.button("Zip terpilih", use_container_width=True) and chosen:
                zpath = root / "arsip.zip"
                with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as zf:
                    for n in chosen:
                        src = root / n
                        if src.exists():
                            zf.write(src, src.name)
                st.success(f"Zip dibuat: {zpath.name}")

    # ------------------------------ perawatan ---------------------------- #
    st.divider()
    with st.expander("Perawatan"):
        st.write(f"yt-dlp terpasang: `{_ver()}` · terbaru: `{latest_ytdlp()}`")
        if st.button("Update yt-dlp sekarang"):
            with st.spinner("Memperbarui..."):
                proc = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-q", "-U", "yt-dlp"],
                    capture_output=True, text=True, timeout=300,
                )
            if proc.returncode == 0:
                st.success("Diperbarui. Restart app agar aktif.")
            else:
                st.error(proc.stderr[-800:])
        days = st.number_input("Hapus file lebih tua dari (hari)", 1, 365, int(cfg["cleanup_days"]))
        cfg["cleanup_days"] = days
        if st.button("Bersihkan file lama"):
            n, freed = storage.cleanup_old(root, int(days))
            st.success(f"{n} file dihapus, {storage.human(freed)} dibebaskan.")
        if st.button("Reset arsip duplikat"):
            try:
                (root / storage.ARCHIVE).unlink()
                st.success("Arsip dihapus.")
            except Exception:
                st.caption("Arsip belum ada.")


def _ver() -> str:
    try:
        import yt_dlp

        return yt_dlp.version.__version__
    except Exception:
        return "?"


if __name__ == "__main__":
    main()
