import json
import os
import sys
from pathlib import Path


def _bundle_dir() -> Path:
    # PyInstaller décompresse les données dans sys._MEIPASS.
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def ui_dir() -> Path:
    return _bundle_dir() / "ui" if hasattr(sys, "_MEIPASS") else Path(__file__).resolve().parent / "ui"


def data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
        path = base / "GhostLoc"
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / "GhostLoc"
    else:
        path = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "ghostloc"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_config() -> dict:
    candidates = [
        _bundle_dir() / "config.json",
        Path(__file__).resolve().parents[2] / "config.json",  # ghostloc/config.json en développement
    ]
    cfg: dict = {}
    for candidate in candidates:
        if candidate.exists():
            cfg = json.loads(candidate.read_text(encoding="utf-8"))
            break
    cfg["supabaseUrl"] = os.environ.get("GHOSTLOC_SUPABASE_URL", cfg.get("supabaseUrl", "")).rstrip("/")
    cfg["supabaseAnonKey"] = os.environ.get("GHOSTLOC_SUPABASE_ANON_KEY", cfg.get("supabaseAnonKey", ""))
    cfg.setdefault("siteUrl", "")
    return cfg
