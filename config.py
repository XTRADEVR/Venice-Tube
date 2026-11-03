"""Konfigurasi persisten (JSON) — pengaturan tidak lagi hilang tiap sesi."""
from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock

BASE = Path(__file__).resolve().parent
CFG_DIR = BASE / ".config"
CFG_FILE = CFG_DIR / "settings.json"
_LOCK = Lock()

DEFAULTS: dict = {
    "dl_dir": os.environ.get("DL_DIR") or str(Path.cwd() / "downloads"),
    "dns_mode": "udp",
    "ipv4_only": True,
    "fragments": 16,
    "chunk": 10485760,
    "rate": 0.0,
    "parallel": int(os.environ.get("MAX_PARALLEL", 2) or 2),
    "compat": True,
    "quality": "best",
    "restrict": False,
    "audio_only": False,
    "subs": False,
    "thumb": False,
    "playlist": False,
    "insecure": False,
    "proxy": "",
    "margin_mb": 500,
    "quota_gb": 0.0,
    "cleanup_days": 30,
    "autoclean": False,
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    with _LOCK:
        if CFG_FILE.exists():
            try:
                raw = json.loads(CFG_FILE.read_text("utf-8"))
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        if k in DEFAULTS and isinstance(v, type(DEFAULTS[k])):
                            cfg[k] = v
                        elif k in DEFAULTS and isinstance(DEFAULTS[k], float) and isinstance(v, int):
                            cfg[k] = float(v)
            except Exception:
                try:
                    CFG_FILE.rename(CFG_FILE.with_suffix(".bad"))
                except Exception:
                    pass
    return cfg


def save(cfg: dict) -> None:
    try:
        CFG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CFG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2), "utf-8")
        tmp.replace(CFG_FILE)
    except Exception:
        pass
