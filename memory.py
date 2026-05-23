import json
import os
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"


def _ensure_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


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
    path = DATA_DIR / "profile.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    return {}


def save_profile(data: dict):
    _ensure_dir()
    profile = load_profile()
    _deep_merge(profile, data)
    with open(DATA_DIR / "profile.json", "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)


# ── Journal ──

def save_journal(entry: dict):
    _ensure_dir()
    entry["_saved_at"] = datetime.now().isoformat()
    with open(DATA_DIR / "journal.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_recent_journal(limit: int = 10) -> list:
    _ensure_dir()
    path = DATA_DIR / "journal.jsonl"
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
    with open(DATA_DIR / "decisions.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_decisions(limit: int = 10) -> list:
    _ensure_dir()
    path = DATA_DIR / "decisions.jsonl"
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
    return {
        "profile": load_profile(),
        "recent_journal": load_recent_journal(),
        "recent_decisions": load_decisions(),
    }
