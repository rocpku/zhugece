import json
import os
import shutil
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
USER_ID = "chenpeng"


# ── 用户管理 ──

def set_user(user_id: str):
    global USER_ID
    USER_ID = user_id


def get_user() -> str:
    return USER_ID


def _user_dir() -> Path:
    return DATA_DIR / USER_ID


def _ensure_dir():
    _user_dir().mkdir(parents=True, exist_ok=True)


def get_user_dir() -> Path:
    return _user_dir()


def _migrate_from_root():
    """从 data/ 迁移到 data/{user_id}/（只迁移一次）"""
    root_profile = DATA_DIR / "profile.json"
    if root_profile.exists():
        dest = _user_dir()
        dest.mkdir(parents=True, exist_ok=True)
        for f in ["profile.json", "journal.jsonl", "decisions.jsonl"]:
            src = DATA_DIR / f
            if src.exists():
                shutil.move(str(src), str(dest / f))
        print(f"[迁移] 已有数据已移至 data/{USER_ID}/")


# ── 工具函数 ──

def _sanitize(obj):
    """递归移除 dict/list/str 中的 surrogate 字符"""
    if isinstance(obj, str):
        return ''.join(ch for ch in obj if not (0xD800 <= ord(ch) <= 0xDFFF))
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


def _safe_json_dumps(obj, **kwargs):
    return json.dumps(_sanitize(obj), **kwargs)


def _safe_json_dump(obj, fp, **kwargs):
    json.dump(_sanitize(obj), fp, **kwargs)


def _deep_merge(base: dict, update: dict):
    if not isinstance(base, dict) or not isinstance(update, dict):
        return
    for key, value in update.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


# ── Profile ──

def load_profile() -> dict:
    _ensure_dir()
    path = _user_dir() / "profile.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    return {}


def save_profile(data: dict):
    _ensure_dir()
    profile = load_profile()
    _deep_merge(profile, data)
    with open(_user_dir() / "profile.json", "w", encoding="utf-8") as f:
        _safe_json_dump(profile, f, ensure_ascii=False, indent=2)


# ── Journal ──

def save_journal(entry: dict):
    _ensure_dir()
    entry["_saved_at"] = datetime.now().isoformat()
    with open(_user_dir() / "journal.jsonl", "a", encoding="utf-8") as f:
        f.write(_safe_json_dumps(entry, ensure_ascii=False) + "\n")


def load_recent_journal(limit: int = 10) -> list:
    _ensure_dir()
    path = _user_dir() / "journal.jsonl"
    if not path.exists():
        return []
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped:
                entries.append(json.loads(stripped))
    return entries[-limit:]


# ── Decisions ──

def save_decision(entry: dict):
    _ensure_dir()
    entry["_saved_at"] = datetime.now().isoformat()
    with open(_user_dir() / "decisions.jsonl", "a", encoding="utf-8") as f:
        f.write(_safe_json_dumps(entry, ensure_ascii=False) + "\n")


def load_decisions(limit: int = 10) -> list:
    _ensure_dir()
    path = _user_dir() / "decisions.jsonl"
    if not path.exists():
        return []
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped:
                entries.append(json.loads(stripped))
    return entries[-limit:]


# ── Load all context for agent ──

def load_full_context() -> dict:
    return _sanitize({
        "profile": load_profile(),
        "recent_journal": load_recent_journal(),
        "recent_decisions": load_decisions(),
    })
