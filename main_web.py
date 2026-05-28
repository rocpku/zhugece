#!/usr/bin/env python3
"""诸葛策 — Web 服务（用户系统 + 多对话）"""

import json
import os
import sys
import io
import sqlite3
import secrets
import hashlib
from pathlib import Path
from datetime import datetime
from typing import Optional
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, Response
import uvicorn

from agent import MingYuanAgent

# ── 数据库 ──

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "web.db"


def get_db():
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            token TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            title TEXT NOT NULL DEFAULT '新对话',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL REFERENCES conversations(id),
            role TEXT NOT NULL,
            content TEXT,
            msg_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(token);
        CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id);
        CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);
    """)
    db.commit()
    db.close()


# ── Agent 管理（按 user_id:conversation_id 缓存）──

_agents: dict[str, MingYuanAgent] = {}


def _agent_key(user_id: int, conv_id: int) -> str:
    return f"{user_id}:{conv_id}"


def get_agent_for_conv(user_id: int, conv_id: int) -> MingYuanAgent:
    key = _agent_key(user_id, conv_id)
    if key not in _agents:
        agent = MingYuanAgent()
        _agents[key] = agent
        # 从数据库恢复消息
        _restore_messages(agent, conv_id)
    return _agents[key]


def _restore_messages(agent: MingYuanAgent, conv_id: int):
    db = get_db()
    rows = db.execute(
        "SELECT msg_json FROM messages WHERE conversation_id=? ORDER BY id",
        (conv_id,)
    ).fetchall()
    db.close()
    agent.messages = []
    for row in rows:
        msg = json.loads(row["msg_json"])
        agent.messages.append(msg)


def _save_message(conv_id: int, role: str, content, msg_dict: dict):
    db = get_db()
    db.execute(
        "INSERT INTO messages (conversation_id, role, content, msg_json, created_at) VALUES (?,?,?,?,?)",
        (conv_id, role, content or "", json.dumps(msg_dict, ensure_ascii=False), datetime.now().isoformat())
    )
    db.commit()
    db.close()


def _update_conv_time(conv_id: int):
    db = get_db()
    db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (datetime.now().isoformat(), conv_id))
    db.commit()
    db.close()


# ── FastAPI ──

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield
    _agents.clear()

app = FastAPI(title="诸葛策", lifespan=lifespan)


# ── 认证辅助 ──

COOKIE_NAME = "zhugece_session"


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def create_session(response: Response, user_id: int):
    token = secrets.token_hex(32)
    db = get_db()
    db.execute(
        "INSERT INTO sessions (user_id, token, created_at) VALUES (?,?,?)",
        (user_id, token, datetime.now().isoformat())
    )
    db.commit()
    db.close()
    response.set_cookie(
        key=COOKIE_NAME, value=token, httponly=True, max_age=86400 * 30,
        samesite="lax", path="/"
    )


def get_current_user(request: Request) -> Optional[dict]:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    db = get_db()
    row = db.execute(
        "SELECT u.id, u.username FROM sessions s JOIN users u ON s.user_id=u.id WHERE s.token=?",
        (token,)
    ).fetchone()
    db.close()
    if row is None:
        return None
    return {"id": row["id"], "username": row["username"]}


def require_user(request: Request) -> dict:
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "请先登录")
    return user


# ── 认证 API ──

@app.post("/api/register")
async def register(request: Request):
    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "").strip()
    if len(username) < 2 or len(password) < 4:
        raise HTTPException(400, "用户名至少2字符，密码至少4字符")

    db = get_db()
    existing = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    if existing:
        db.close()
        raise HTTPException(400, "用户名已存在")
    db.execute(
        "INSERT INTO users (username, password_hash, created_at) VALUES (?,?,?)",
        (username, hash_password(password), datetime.now().isoformat())
    )
    db.commit()
    user_id = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()["id"]
    db.close()

    resp = Response(json.dumps({"ok": True}, ensure_ascii=False), media_type="application/json")
    create_session(resp, user_id)
    return resp


@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "").strip()

    db = get_db()
    row = db.execute(
        "SELECT id, username FROM users WHERE username=? AND password_hash=?",
        (username, hash_password(password))
    ).fetchone()
    db.close()
    if not row:
        raise HTTPException(401, "用户名或密码错误")

    resp = Response(json.dumps({"ok": True, "username": row["username"]}, ensure_ascii=False), media_type="application/json")
    create_session(resp, row["id"])
    return resp


@app.post("/api/logout")
async def logout():
    resp = Response(json.dumps({"ok": True}), media_type="application/json")
    resp.set_cookie(key=COOKIE_NAME, value="", httponly=True, max_age=0, path="/")
    return resp


@app.get("/api/me")
async def me(request: Request):
    user = get_current_user(request)
    if not user:
        return {"authenticated": False}
    return {"authenticated": True, "username": user["username"]}


# ── 对话管理 API ──

@app.get("/api/conversations")
async def list_conversations(request: Request):
    user = require_user(request)
    db = get_db()
    rows = db.execute(
        "SELECT id, title, created_at, updated_at FROM conversations WHERE user_id=? ORDER BY updated_at DESC",
        (user["id"],)
    ).fetchall()
    db.close()
    return [{"id": r["id"], "title": r["title"], "created_at": r["created_at"], "updated_at": r["updated_at"]} for r in rows]


@app.post("/api/conversations")
async def create_conversation(request: Request):
    user = require_user(request)
    now = datetime.now().isoformat()
    db = get_db()
    cur = db.execute(
        "INSERT INTO conversations (user_id, title, created_at, updated_at) VALUES (?,?,?,?)",
        (user["id"], "新对话", now, now)
    )
    db.commit()
    conv_id = cur.lastrowid
    db.close()
    return {"id": conv_id, "title": "新对话"}


@app.delete("/api/conversations/{conv_id}")
async def delete_conversation(conv_id: int, request: Request):
    user = require_user(request)
    db = get_db()
    conv = db.execute("SELECT id FROM conversations WHERE id=? AND user_id=?", (conv_id, user["id"])).fetchone()
    if not conv:
        db.close()
        raise HTTPException(404, "对话不存在")
    db.execute("DELETE FROM messages WHERE conversation_id=?", (conv_id,))
    db.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
    db.commit()
    db.close()
    # 清除内存中的 agent
    _agents.pop(_agent_key(user["id"], conv_id), None)
    return {"ok": True}


@app.get("/api/conversations/{conv_id}/history")
async def conversation_history(conv_id: int, request: Request):
    user = require_user(request)
    # 验证属于当前用户
    db = get_db()
    conv = db.execute("SELECT id FROM conversations WHERE id=? AND user_id=?", (conv_id, user["id"])).fetchone()
    if not conv:
        db.close()
        raise HTTPException(404, "对话不存在")
    rows = db.execute(
        "SELECT role, content FROM messages WHERE conversation_id=? AND role IN ('user','assistant') AND content != '' ORDER BY id",
        (conv_id,)
    ).fetchall()
    db.close()
    result = []
    for r in rows:
        result.append({"role": r["role"], "content": r["content"]})
    return result


# ── 聊天 API（SSE 流式）──

@app.post("/api/chat")
async def chat(request: Request):
    user = require_user(request)
    body = await request.json()
    user_message = body.get("message", "").strip()
    conv_id = body.get("conversation_id", 0)

    if not user_message:
        raise HTTPException(400, "message is required")

    db = get_db()

    # 验证对话属于用户，或自动创建新对话
    if conv_id:
        conv = db.execute("SELECT id, title FROM conversations WHERE id=? AND user_id=?", (conv_id, user["id"])).fetchone()
        if not conv:
            db.close()
            raise HTTPException(404, "对话不存在")
        # 首条消息时自动更新标题
        if conv["title"] == "新对话":
            title = user_message[:30].strip()
            if len(title) > 0:
                db.execute("UPDATE conversations SET title=? WHERE id=?", (title, conv_id))
                db.commit()
    else:
        # 自动创建新对话
        now = datetime.now().isoformat()
        title = user_message[:30].strip() or "新对话"
        cur = db.execute(
            "INSERT INTO conversations (user_id, title, created_at, updated_at) VALUES (?,?,?,?)",
            (user["id"], title, now, now)
        )
        db.commit()
        conv_id = cur.lastrowid

    db.close()

    # 获取 agent（并设置当前用户，确保 memory 模块读写正确目录）
    from memory import set_user as _set_user
    _set_user(user["username"])
    agent = get_agent_for_conv(user["id"], conv_id)

    # 保存用户消息
    _save_message(conv_id, "user", user_message, {"role": "user", "content": user_message})
    _update_conv_time(conv_id)

    async def event_stream():
        full_response = ""
        try:
            for msg_type, content in agent.chat(user_message):
                if await request.is_disconnected():
                    break
                if msg_type == "text":
                    full_response += content
                data = json.dumps({"type": msg_type, "content": content}, ensure_ascii=False)
                yield f"data: {data}\n\n"
            yield "data: [DONE]\n\n"

            # 保存 assistant 回复
            if full_response:
                _save_message(conv_id, "assistant", full_response, {"role": "assistant", "content": full_response})
                _update_conv_time(conv_id)
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── 用户画像 API（基于登录用户）──

@app.get("/api/profile")
async def profile(request: Request):
    user = require_user(request)
    from memory import set_user, load_profile
    set_user(user["username"])
    p = load_profile()
    if not p:
        return {"exists": False}
    return {"exists": True, "name": p.get("basic", {}).get("name", user["username"])}


# ── 静态页面 ──

HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>诸葛策 — 个人战略引擎</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@400;600;700&family=Noto+Sans+SC:wght@300;400;500&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #f6f4ef;
  --surface: #fffcf8;
  --ink: #3a3530;
  --ink-light: #6b6258;
  --ink-lighter: #9e9488;
  --border: #e6dfd6;
  --border-light: #f0ebe4;
  --accent: #b8925a;
  --accent-light: #e8d5b8;
  --user-msg: #3a3530;
  --user-msg-text: #f5f1eb;
  --shadow: rgba(58,53,48,0.06);
  --font-heading: "Noto Serif SC", "Songti SC", serif;
  --font-body: "Noto Sans SC", -apple-system, sans-serif;
}
* { margin:0; padding:0; box-sizing:border-box; }
html, body { height:100%; }
body {
  font-family: var(--font-body);
  background: var(--bg);
  background-image: radial-gradient(ellipse at 10% 30%, rgba(184,146,90,0.04) 0%, transparent 50%);
  color: var(--ink);
  height: 100vh;
  display: flex;
  flex-direction: column;
}

/* ── Auth page ── */
.auth-page {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100vh;
  padding: 20px;
}
.auth-card {
  background: var(--surface);
  padding: 48px 40px 36px;
  border-radius: 16px;
  box-shadow: 0 2px 24px var(--shadow);
  width: 100%;
  max-width: 380px;
  text-align: center;
  border: 1px solid var(--border-light);
}
.auth-card h1 {
  font-family: var(--font-heading);
  font-size: 22px;
  font-weight: 700;
  letter-spacing: 0.12em;
  margin-bottom: 4px;
}
.auth-card .sub {
  color: var(--ink-lighter);
  font-size: 13px;
  margin-bottom: 28px;
  letter-spacing: 0.06em;
}
.auth-card input {
  width: 100%;
  padding: 10px 14px;
  border: 1px solid var(--border);
  border-radius: 8px;
  font-size: 14px;
  font-family: inherit;
  outline: none;
  margin-bottom: 12px;
  transition: border-color 0.15s;
  background: var(--bg);
}
.auth-card input:focus { border-color: var(--accent); background: #fff; }
.auth-card button {
  width: 100%;
  padding: 10px;
  background: var(--ink);
  color: #fff;
  border: none;
  border-radius: 8px;
  font-size: 14px;
  cursor: pointer;
  margin-top: 4px;
  transition: background 0.15s;
}
.auth-card button:hover { background: #555; }
.auth-card .toggle {
  margin-top: 16px;
  font-size: 13px;
  color: var(--ink-lighter);
  cursor: pointer;
}
.auth-card .toggle:hover { color: var(--accent); }
.auth-card .err {
  color: #c33;
  font-size: 13px;
  margin-bottom: 12px;
  display: none;
}
.auth-card .seal {
  width: 48px; height: 48px;
  border: 2px solid var(--accent);
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-family: var(--font-heading);
  font-size: 20px;
  color: var(--accent);
  margin-bottom: 16px;
}

/* ── Dashboard layout ── */
.app { display: none; height: 100vh; flex-direction: column; }
.app.visible { display: flex; }

.top-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 20px;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.top-bar h1 {
  font-family: var(--font-heading);
  font-size: 16px;
  font-weight: 600;
  letter-spacing: 0.1em;
}
.top-bar .user-info {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
  color: var(--ink-light);
}
.top-bar .logout-btn {
  background: none;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 4px 10px;
  font-size: 12px;
  color: var(--ink-lighter);
  cursor: pointer;
  font-family: inherit;
}
.top-bar .logout-btn:hover { border-color: var(--accent); color: var(--accent); }

/* ── Chat layout ── */
.chat-layout {
  display: flex;
  flex: 1;
  overflow: hidden;
}

/* Sidebar */
.sidebar {
  width: 240px;
  background: var(--surface);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
}
.sidebar .new-btn {
  margin: 12px;
  padding: 8px;
  background: var(--bg);
  border: 1px dashed var(--border);
  border-radius: 8px;
  font-size: 13px;
  color: var(--ink-light);
  cursor: pointer;
  font-family: inherit;
  transition: border-color 0.15s;
}
.sidebar .new-btn:hover { border-color: var(--accent); color: var(--accent); }
.sidebar .conv-list {
  flex: 1;
  overflow-y: auto;
  padding: 0 8px 8px;
}
.sidebar .conv-item {
  display: flex; align-items: center;
  padding: 8px 10px;
  border-radius: 6px;
  font-size: 13px;
  color: var(--ink-light);
  cursor: pointer;
  transition: background 0.1s;
}
.sidebar .conv-item:hover { background: var(--bg); }
.sidebar .conv-item.active { background: var(--accent-light); color: var(--ink); font-weight: 500; }
.sidebar .conv-item .ct { flex:1; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.sidebar .conv-item .cd { display:none; background:none; border:none; font-size:14px; color:var(--ink-lighter); cursor:pointer; padding:0 2px 0 6px; line-height:1; font-family:inherit; opacity:0.5; }
.sidebar .conv-item:hover .cd { display:block; }
.sidebar .conv-item .cd:hover { opacity:1; color:#c33; }

/* Main chat */
.chat-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.messages {
  flex: 1;
  overflow-y: auto;
  padding: 24px 20px 16px;
  display: flex;
  flex-direction: column;
  gap: 18px;
}
.messages::-webkit-scrollbar { width: 4px; }
.messages::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }

.msg { max-width: 680px; width: fit-content; animation: msgIn 0.3s ease; }
@keyframes msgIn { from { opacity:0; transform:translateY(8px); } to { opacity:1; transform:translateY(0); } }
.msg.user {
  align-self: flex-end;
  background: var(--user-msg);
  color: var(--user-msg-text);
  border-radius: 16px 16px 4px 16px;
  padding: 10px 18px;
  font-size: 14px;
  line-height: 1.65;
  max-width: 70%;
}
.msg.agent {
  align-self: flex-start;
  background: var(--surface);
  border-radius: 16px 16px 16px 4px;
  padding: 16px 22px;
  font-size: 14px;
  line-height: 1.85;
  box-shadow: 0 1px 4px var(--shadow);
  border: 1px solid var(--border-light);
  color: var(--ink);
}
.msg.agent p { margin:0.5em 0; }
.msg.agent p:first-child { margin-top:0; }
.msg.agent p:last-child { margin-bottom:0; }
.msg.agent strong { color: #3a3530; font-weight:600; }
.msg.agent em { color: var(--accent); font-style:normal; font-weight:500; }
.msg.agent h1, .msg.agent h2, .msg.agent h3 {
  font-family: var(--font-heading);
  margin:1em 0 0.4em;
  font-weight:600;
  letter-spacing:0.02em;
}
.msg.agent h1 { font-size:1.15rem; }
.msg.agent h2 { font-size:1.05rem; border-bottom:1px solid var(--border-light); padding-bottom:0.3em; }
.msg.agent h3 { font-size:0.95rem; }
.msg.agent ul, .msg.agent ol { margin:0.4em 0 0.4em 1.3em; }
.msg.agent li { margin:0.2em 0; }
.msg.agent blockquote { margin:0.6em 0; padding:0.4em 0.8em 0.4em 1em; border-left:2px solid var(--accent-light); color:var(--ink-light); font-size:0.9em; }
.msg.agent code { background: var(--bg); padding:0.1em 0.4em; border-radius:4px; font-size:0.9em; color:var(--ink-light); }
.msg.agent pre { background: var(--bg); padding:12px 16px; border-radius:8px; overflow-x:auto; font-size:0.85rem; margin:0.6em 0; border:1px solid var(--border-light); }
.msg.agent table { width:100%; border-collapse:collapse; margin:0.6em 0; font-size:0.85rem; }
.msg.agent th, .msg.agent td { padding:6px 10px; text-align:left; border-bottom:1px solid var(--border-light); }
.msg.agent th { background:var(--bg); font-weight:500; }
.msg.agent hr { border:none; border-top:1px solid var(--border-light); margin:1em 0; }
.msg.agent .tool-call { display:inline-flex; align-items:center; gap:6px; font-size:12px; color:var(--ink-lighter); background:var(--bg); padding:3px 10px; border-radius:6px; margin:4px 0; }
.msg.agent .tool-call::before { content:''; width:12px; height:12px; border:1.5px solid var(--accent-light); border-top-color:var(--accent); border-radius:50%; animation:spin 0.8s linear infinite; }
@keyframes spin { to { transform:rotate(360deg); } }

.thinking-dots { display:inline-flex; align-items:center; gap:3px; padding:4px 0; }
.thinking-dots span { width:5px; height:5px; background:var(--ink-lighter); border-radius:50%; animation:dotPulse 1.2s ease-in-out infinite; }
.thinking-dots span:nth-child(2) { animation-delay:0.2s; }
.thinking-dots span:nth-child(3) { animation-delay:0.4s; }
@keyframes dotPulse { 0%,80%,100% { transform:scale(0.6); opacity:0.3; } 40% { transform:scale(1); opacity:0.8; } }

/* Welcome in chat */
.welcome { text-align:center; margin:60px 20px; }

/* Input area */
.input-area {
  flex-shrink:0;
  background:var(--surface);
  border-top:1px solid var(--border);
  padding:10px 16px 14px;
}
.input-area .input-wrap {
  display:flex;
  align-items:flex-end;
  gap:8px;
  background:var(--bg);
  border:1px solid var(--border);
  border-radius:12px;
  padding:6px 8px 6px 16px;
  transition:border-color 0.15s, box-shadow 0.15s;
}
.input-area .input-wrap:focus-within { border-color:var(--accent); box-shadow:0 0 0 2px rgba(184,146,90,0.1); background:#fff; }
.input-area textarea {
  flex:1;
  border:none;
  padding:6px 0;
  font-size:14px;
  font-family:var(--font-body);
  resize:none;
  outline:none;
  max-height:112px;
  line-height:1.6;
  background:transparent;
  color:var(--ink);
}
.input-area textarea::placeholder { color:var(--ink-lighter); font-weight:300; }
.input-area button {
  background:var(--accent);
  color:#fff;
  border:none;
  border-radius:8px;
  width:36px;
  height:36px;
  display:flex;
  align-items:center;
  justify-content:center;
  cursor:pointer;
  transition:background 0.15s, transform 0.1s;
  flex-shrink:0;
}
.input-area button:hover { background:#a37d4a; }
.input-area button:active { transform:scale(0.95); }
.input-area button:disabled { background:var(--border); cursor:not-allowed; transform:none; }
.input-area button svg { width:16px; height:16px; fill:currentColor; }
.input-area.loading .input-wrap { opacity:0.6; }
.input-area.loading button { background:var(--ink-lighter); }

@media (max-width:700px) {
  .sidebar { display:none; }
  .sidebar.open { display:flex; position:fixed; left:0; top:0; bottom:0; z-index:10; width:260px; box-shadow:4px 0 20px rgba(0,0,0,0.1); }
}
</style>
</head>
<body>

<!-- ── Auth ── -->
<div class="auth-page" id="authPage">
  <div class="auth-card">
    <div class="seal">策</div>
    <h1>诸葛策</h1>
    <div class="sub">个人战略引擎</div>
    <div class="err" id="authErr"></div>
    <input type="text" id="authUser" placeholder="用户名" autocomplete="username">
    <input type="password" id="authPass" placeholder="密码" autocomplete="current-password">
    <button id="authBtn">登录</button>
    <div class="toggle" id="authToggle">没有账号？去注册</div>
  </div>
</div>

<!-- ── App ── -->
<div class="app" id="app">
  <div class="top-bar">
    <div style="display:flex;align-items:center;gap:8px;cursor:pointer" id="menuBtn">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="var(--ink-lighter)" stroke-width="2"><path d="M3 12h18M3 6h18M3 18h18"/></svg>
    </div>
    <h1>诸葛策</h1>
    <div class="user-info">
      <span id="userName"></span>
      <button class="logout-btn" id="logoutBtn">退出</button>
    </div>
  </div>
  <div class="chat-layout">
    <div class="sidebar" id="sidebar">
      <button class="new-btn" id="newConvBtn">+ 新对话</button>
      <div class="conv-list" id="convList"></div>
    </div>
    <div class="chat-main">
      <div class="messages" id="messages"></div>
      <div class="input-area" id="inputArea">
        <div class="input-wrap">
          <textarea id="input" rows="1" placeholder="输入你的问题…"></textarea>
          <button id="sendBtn" title="发送"><svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg></button>
        </div>
      </div>
    </div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/marked@15.0.7/marked.min.js"></script>
<script>
// ── State ──
let currentConvId = 0;
let convs = [];
let loading = false;

const $ = id => document.getElementById(id);
const msgEl = $('messages');
const inputEl = $('input');
const sendBtn = $('sendBtn');
const inputArea = $('inputArea');

// ── Auth ──
let authMode = 'login';

$('authToggle').onclick = () => {
  authMode = authMode === 'login' ? 'register' : 'login';
  $('authBtn').textContent = authMode === 'login' ? '登录' : '注册';
  $('authToggle').textContent = authMode === 'login' ? '没有账号？去注册' : '已有账号？去登录';
  $('authErr').style.display = 'none';
};

$('authBtn').onclick = async () => {
  const u = $('authUser').value.trim();
  const p = $('authPass').value.trim();
  if (!u || !p) return;
  $('authErr').style.display = 'none';
  const resp = await fetch('/api/' + authMode, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({username:u, password:p}),
  });
  if (!resp.ok) {
    const err = await resp.json();
    $('authErr').textContent = err.detail || '操作失败';
    $('authErr').style.display = 'block';
    return;
  }
  initApp();
};

// Enter to submit auth forms
$('authPass').onkeydown = e => { if (e.key === 'Enter') $('authBtn').click(); };
$('authUser').onkeydown = e => { if (e.key === 'Enter') $('authPass').focus(); };

// ── App Init ──
async function initApp() {
  $('authPage').style.display = 'none';
  $('app').classList.add('visible');
  const me = await (await fetch('/api/me')).json();
  $('userName').textContent = me.username;
  await loadConvs();
  // 默认加载最新对话
  if (convs.length > 0) {
    await selectConv(convs[0].id);
  } else {
    showWelcome(me.username);
  }
}

// ── Conversations ──
async function loadConvs() {
  convs = await (await fetch('/api/conversations')).json();
  renderConvList();
}

function renderConvList() {
  $('convList').innerHTML = convs.map(c =>
    '<div class="conv-item' + (c.id === currentConvId ? ' active' : '') + '" data-id="' + c.id + '">' +
      '<span class="ct">' + escapeHtml(c.title) + '</span>' +
      '<button class="cd" data-id="' + c.id + '" title="删除对话">×</button>' +
    '</div>'
  ).join('');
  $('convList').querySelectorAll('.conv-item').forEach(el => {
    el.onclick = (e) => { if (!e.target.classList.contains('cd')) selectConv(parseInt(el.dataset.id)); };
  });
  $('convList').querySelectorAll('.cd').forEach(btn => {
    btn.onclick = async (e) => { e.stopPropagation();
      const id = parseInt(btn.dataset.id);
      if (!confirm('确定删除此对话？')) return;
      await fetch('/api/conversations/' + id, {method:'DELETE'});
      convs = convs.filter(c => c.id !== id);
      if (currentConvId === id) { currentConvId = 0; msgEl.innerHTML = ''; }
      renderConvList();
    };
  });
}

$('newConvBtn').onclick = async () => {
  const resp = await fetch('/api/conversations', {method:'POST'});
  const conv = await resp.json();
  convs.unshift(conv);
  await selectConv(conv.id);
  renderConvList();
};

async function selectConv(convId) {
  currentConvId = convId;
  renderConvList();
  msgEl.innerHTML = '';
  // 加载历史
  const msgs = await (await fetch('/api/conversations/' + convId + '/history')).json();
  if (msgs.length > 0) {
    for (const m of msgs) {
      if (m.role === 'user') addMessage('user', escapeHtml(m.content));
      else addMessage('agent', marked.parse(m.content));
    }
  } else {
    showWelcome(null);
  }
}

function showWelcome(name) {
  msgEl.innerHTML = '<div class="welcome">' +
    (name ? '<div style="font-family:var(--font-heading);font-size:20px;color:var(--accent);margin-bottom:10px;">' + escapeHtml(name) + '，你好</div>' : '') +
    '<div style="color:var(--ink-lighter);font-size:13px;line-height:1.8;letter-spacing:0.04em;">谋定而后动，知止而有得。</div></div>';
}

// ── Chat ──
function addMessage(role, content) {
  const div = document.createElement('div');
  div.className = 'msg ' + role;
  div.innerHTML = content;
  msgEl.appendChild(div);
  requestAnimationFrame(() => msgEl.scrollTop = msgEl.scrollHeight);
  return div;
}

function escapeHtml(t) {
  const d = document.createElement('div');
  d.textContent = t;
  return d.innerHTML;
}

inputEl.oninput = () => {
  inputEl.style.height = 'auto';
  inputEl.style.height = Math.min(inputEl.scrollHeight, 112) + 'px';
};
inputEl.onkeydown = e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
};

async function send() {
  const text = inputEl.value.trim();
  if (!text || loading) return;
  if (!currentConvId) {
    // 自动创建新对话
    const resp = await fetch('/api/conversations', {method:'POST'});
    const conv = await resp.json();
    convs.unshift(conv);
    currentConvId = conv.id;
    renderConvList();
  }

  inputEl.value = '';
  inputEl.style.height = 'auto';
  msgEl.querySelector('.welcome')?.remove();

  addMessage('user', escapeHtml(text));
  loading = true;
  inputArea.classList.add('loading');
  sendBtn.disabled = true;
  inputEl.disabled = true;

  const dots = '<div class="thinking-dots"><span></span><span></span><span></span></div>';
  const msgDiv = addMessage('agent', dots);
  let content = '';

  try {
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({message: text, conversation_id: currentConvId}),
    });
    if (!resp.ok) throw new Error('请求失败');
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      for (const line of decoder.decode(value, {stream:true}).split('\n')) {
        if (!line.startsWith('data: ')) continue;
        const data = line.slice(6);
        if (data === '[DONE]') break;
        try {
          const p = JSON.parse(data);
          if (p.type === 'text') { content += p.content; msgDiv.innerHTML = marked.parse(content); }
          else if (p.type === 'tool_start') { content += '\n\n<div class="tool-call">' + escapeHtml(p.content) + '</div>\n'; msgDiv.innerHTML = marked.parse(content); }
        } catch(e) {}
      }
      requestAnimationFrame(() => msgEl.scrollTop = msgEl.scrollHeight);
    }
  } catch(e) {
    msgDiv.innerHTML = '<p style="color:#b33;">连接失败，请确认服务器正在运行</p>';
  }

  loading = false;
  inputArea.classList.remove('loading');
  sendBtn.disabled = false;
  inputEl.disabled = false;
  inputEl.focus();

  // 重新加载对话列表（可能标题变了）
  loadConvs();
}

// ── 初始检查 ──
async function checkAuth() {
  const me = await (await fetch('/api/me')).json();
  if (me.authenticated) {
    initApp();
  }
}

// ── 登出 ──
$('logoutBtn').onclick = async () => {
  await fetch('/api/logout', {method:'POST'});
  location.reload();
};

// ── Sidebar toggle (mobile) ──
$('menuBtn').onclick = () => $('sidebar').classList.toggle('open');

checkAuth();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML


# ── 启动 ──

def main():
    port = int(os.getenv("PORT", "8080"))
    print(f"诸葛策 Web 服务 → http://localhost:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
