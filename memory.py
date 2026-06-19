import json
from datetime import datetime

from db import get_db


def set_user(user_id: str):
    global _USER_ID
    _USER_ID = user_id


def get_user() -> str:
    return _USER_ID


_USER_ID = "default"


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
    db = get_db()
    row = db.execute("SELECT data FROM profiles WHERE user_id=%s", (_USER_ID,)).fetchone()
    # 修复前的数据存在 user_id='default' 下，做回退读取
    if not row and _USER_ID != "default":
        row = db.execute("SELECT data FROM profiles WHERE user_id='default'").fetchone()
    db.close()
    if row:
        data = json.loads(row["data"])
        return data if isinstance(data, dict) else {}
    return {}


def save_profile(data: dict):
    db = get_db()
    existing = db.execute("SELECT data FROM profiles WHERE user_id=%s", (_USER_ID,)).fetchone()
    if existing:
        profile = json.loads(existing["data"]) if isinstance(existing["data"], str) else {}
        _deep_merge(profile, data)
        db.execute(
            "UPDATE profiles SET data=%s, updated_at=%s WHERE user_id=%s",
            (_safe_json_dumps(profile, ensure_ascii=False), datetime.now().isoformat(), _USER_ID)
        )
    else:
        db.execute(
            "INSERT INTO profiles (user_id, data, updated_at) VALUES (%s,%s,%s)",
            (_USER_ID, _safe_json_dumps(data, ensure_ascii=False), datetime.now().isoformat())
        )
    db.commit()
    db.close()


# ── Journal ──

def save_journal(entry: dict):
    entry["_saved_at"] = datetime.now().isoformat()
    db = get_db()
    db.execute(
        "INSERT INTO journal (user_id, entry_json, saved_at) VALUES (%s,%s,%s)",
        (_USER_ID, _safe_json_dumps(entry, ensure_ascii=False), datetime.now().isoformat())
    )
    db.commit()
    db.close()


def load_recent_journal(limit: int = 10) -> list:
    db = get_db()
    rows = db.execute(
        "SELECT entry_json FROM journal WHERE user_id=%s ORDER BY id DESC LIMIT %s",
        (_USER_ID, limit)
    ).fetchall()
    db.close()
    entries = []
    for row in reversed(rows):
        entries.append(json.loads(row["entry_json"]))
    return entries


# ── Decisions ──

def save_decision(entry: dict):
    entry["_saved_at"] = datetime.now().isoformat()
    db = get_db()
    db.execute(
        "INSERT INTO decisions (user_id, entry_json, saved_at) VALUES (%s,%s,%s)",
        (_USER_ID, _safe_json_dumps(entry, ensure_ascii=False), datetime.now().isoformat())
    )
    db.commit()
    db.close()


def load_decisions(limit: int = 10) -> list:
    db = get_db()
    rows = db.execute(
        "SELECT entry_json FROM decisions WHERE user_id=%s ORDER BY id DESC LIMIT %s",
        (_USER_ID, limit)
    ).fetchall()
    db.close()
    entries = []
    for row in reversed(rows):
        entries.append(json.loads(row["entry_json"]))
    return entries


# ── Load all context for agent ──

def load_full_context() -> dict:
    return _sanitize({
        "profile": load_profile(),
        "recent_journal": load_recent_journal(),
        "recent_decisions": load_decisions(),
    })