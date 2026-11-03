"""Utilitas folder, nama file, ruang disk, dan daftar media."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

MEDIA_EXT = {
    ".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".ts", ".m4v",
    ".mp3", ".m4a", ".opus", ".ogg", ".flac", ".wav",
    ".srt", ".vtt", ".ass", ".jpg", ".png", ".webp", ".json", ".nfo",
}
INCOMING = ".incoming"
ARCHIVE = ".downloaded.txt"
BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


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
    return "?"


def human_time(sec) -> str:
    try:
        sec = int(sec)
    except (TypeError, ValueError):
        return "--:--"
    if sec < 0:
        return "--:--"
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def prepare(root: Path) -> tuple[bool, str]:
    try:
        root.mkdir(parents=True, exist_ok=True)
        (root / INCOMING).mkdir(exist_ok=True)
        probe = root / ".write_test"
        probe.write_bytes(b"1")
        probe.unlink()
        return True, ""
    except Exception as exc:
        return False, str(exc)


def free_bytes(path: Path) -> int:
    try:
        return shutil.disk_usage(path).free
    except Exception:
        return 0


def site_of(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url or "")
    if not m:
        return "web"
    host = m.group(1).lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m."):
        host = host[2:]
    return BAD.sub("", host)[:40] or "web"


def sanitize(text: str, restrict: bool = False, limit: int = 100) -> str:
    text = BAD.sub(" ", text or "")
    text = re.sub(r"\s+", " ", text).strip(" .")
    if restrict:
        text = re.sub(r"[^A-Za-z0-9 ._-]", "", text)
        text = re.sub(r"\s+", "_", text.strip())
    return (text[:limit].rstrip(" .") or "video")


def build_name(site: str, title: str, vid: str, restrict: bool = False) -> str:
    tag = f"[{sanitize(vid, True, 40)}]" if vid else ""
    return f"{sanitize(site, True, 40)} - {sanitize(title, restrict)}{' ' + tag if tag else ''}"


def unique_path(folder: Path, stem: str, ext: str) -> Path:
    ext = ext if ext.startswith(".") else f".{ext}"
    cand = folder / f"{stem}{ext}"
    i = 2
    while cand.exists():
        cand = folder / f"{stem} ({i}){ext}"
        i += 1
    return cand


def find_existing(folder: Path, vid: str) -> Path | None:
    if not vid:
        return None
    tag = f"[{sanitize(vid, True, 40)}]"
    try:
        for p in folder.iterdir():
            if p.is_file() and tag in p.name and not p.name.endswith((".part", ".ytdl")):
                return p
    except Exception:
        pass
    return None


def move_in(paths: list[Path], dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    moved = []
    for src in paths:
        try:
            target = unique_path(dest, src.stem, src.suffix)
            shutil.move(str(src), str(target))
            moved.append(target)
        except Exception:
            continue
    return moved


def collect_new(folder: Path, since: float) -> list[Path]:
    out = []
    try:
        for p in folder.iterdir():
            if p.is_file() and p.stat().st_mtime >= since and not p.name.endswith(".part"):
                out.append(p)
    except Exception:
        pass
    return out


def list_media(folder: Path, site: str = "", sort: str = "terbaru") -> list[Path]:
    items = []
    try:
        for p in folder.iterdir():
            if p.is_file() and p.suffix.lower() in MEDIA_EXT and p.name != ARCHIVE:
                if site and site not in p.name:
                    continue
                items.append(p)
    except Exception:
        return []
    keys = {
        "terbaru": lambda p: p.stat().st_mtime,
        "terlama": lambda p: p.stat().st_mtime,
        "terbesar": lambda p: p.stat().st_size,
        "nama": lambda p: p.name.lower(),
    }
    return sorted(items, key=keys.get(sort, keys["terbaru"]), reverse=sort in ("terbaru", "terbesar"))


def cleanup_old(folder: Path, days: int) -> tuple[int, int]:
    import time as _t
    cutoff = _t.time() - days * 86400
    count = freed = 0
    try:
        for p in folder.iterdir():
            if p.is_file() and p.stat().st_mtime < cutoff:
                size = p.stat().st_size
                p.unlink()
                count += 1
                freed += size
    except Exception:
        pass
    return count, freed
