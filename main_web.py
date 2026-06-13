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
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import httpx
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
            password_hash TEXT,
            wechat_openid TEXT UNIQUE,
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
    # 兼容已有数据库：新增列（不能带 UNIQUE 约束，否则已有数据会报错）
    cols = [row["name"] for row in db.execute("PRAGMA table_info(users)").fetchall()]
    if "wechat_openid" not in cols:
        db.execute("ALTER TABLE users ADD COLUMN wechat_openid TEXT")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_wechat_openid ON users(wechat_openid)")
    if "password_hash" not in cols:
        db.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
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
    os.environ["ZHUGE_WEB_URL"] = "1"
    yield
    _agents.clear()

app = FastAPI(title="诸葛策", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/renders", StaticFiles(directory=str(Path(__file__).parent / "renders")), name="renders")


# ── 认证辅助 ──

COOKIE_NAME = "zhugece_session"


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def create_session(response: Optional[Response], user_id: int) -> str:
    token = secrets.token_hex(32)
    db = get_db()
    db.execute(
        "INSERT INTO sessions (user_id, token, created_at) VALUES (?,?,?)",
        (user_id, token, datetime.now().isoformat())
    )
    db.commit()
    db.close()
    if response is not None:
        response.set_cookie(
            key=COOKIE_NAME, value=token, httponly=True, max_age=86400 * 30,
            samesite="lax", path="/"
        )
    return token


_DEFAULT_USER_PREFIX = "wx_"


def get_current_user(request: Request) -> Optional[dict]:
    # Try cookie first, then Authorization header
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
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


@app.post("/api/wx-login")
async def wx_login(request: Request):
    """微信小程序登录：接收 code，调用微信 jscode2session，返回 token"""
    body = await request.json()
    code = body.get("code", "").strip()
    if not code:
        raise HTTPException(400, "code is required")

    appid = os.getenv("WECHAT_APPID", "")
    secret = os.getenv("WECHAT_SECRET", "")
    if not appid or not secret:
        raise HTTPException(500, "服务器未配置微信登录")

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.weixin.qq.com/sns/jscode2session",
            params={
                "appid": appid,
                "secret": secret,
                "js_code": code,
                "grant_type": "authorization_code",
            },
        )
    wx_data = resp.json()
    if "openid" not in wx_data:
        raise HTTPException(400, f"微信登录失败: {wx_data.get('errmsg', '未知错误')}")

    openid = wx_data["openid"]
    db = get_db()

    # 查找已有用户
    row = db.execute("SELECT id, username FROM users WHERE wechat_openid=?", (openid,)).fetchone()
    is_new = False
    if row:
        user_id = row["id"]
        username = row["username"]
    else:
        # 创建新用户（password_hash 设为空占位，因为表有 NOT NULL 约束）
        username = f"{_DEFAULT_USER_PREFIX}{openid[-8:]}"
        now = datetime.now().isoformat()
        cur = db.execute(
            "INSERT INTO users (username, password_hash, wechat_openid, created_at) VALUES (?,?,?,?)",
            (username, "", openid, now)
        )
        db.commit()
        user_id = cur.lastrowid
        is_new = True
    db.close()

    token = create_session(None, user_id)
    return {"token": token, "is_new_user": is_new, "username": username}


@app.post("/api/dev-login")
async def dev_login(request: Request):
    """开发测试：直接返回一个 token（免微信登录），支持指定用户名"""
    try:
        body = await request.json()
        username = (body.get("username") or "").strip()
    except Exception:
        username = ""
    if not username:
        username = "dev_user"

    now = datetime.now().isoformat()
    db = get_db()
    row = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    if row:
        user_id = row["id"]
    else:
        cur = db.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?,?,?)",
            (username, "", now)
        )
        db.commit()
        user_id = cur.lastrowid
    db.close()

    token = create_session(None, user_id)
    return {"token": token, "user_id": user_id, "username": username}


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


# ── 非流式聊天接口（适合小程序等不支持 SSE 的客户端）──

@app.post("/api/send")
async def send_message(request: Request):
    user = require_user(request)
    body = await request.json()
    user_message = body.get("message", "").strip()
    conv_id = body.get("conversation_id", 0)

    if not user_message:
        raise HTTPException(400, "message is required")

    db = get_db()

    if conv_id:
        conv = db.execute("SELECT id, title FROM conversations WHERE id=? AND user_id=?", (conv_id, user["id"])).fetchone()
        if not conv:
            db.close()
            raise HTTPException(404, "对话不存在")
        if conv["title"] == "新对话":
            title = user_message[:30].strip()
            if len(title) > 0:
                db.execute("UPDATE conversations SET title=? WHERE id=?", (title, conv_id))
                db.commit()
    else:
        now = datetime.now().isoformat()
        title = user_message[:30].strip() or "新对话"
        cur = db.execute(
            "INSERT INTO conversations (user_id, title, created_at, updated_at) VALUES (?,?,?,?)",
            (user["id"], title, now, now)
        )
        db.commit()
        conv_id = cur.lastrowid

    db.close()

    from memory import set_user as _set_user
    _set_user(user["username"])
    agent = get_agent_for_conv(user["id"], conv_id)

    _save_message(conv_id, "user", user_message, {"role": "user", "content": user_message})
    _update_conv_time(conv_id)

    full_response = ""
    try:
        for msg_type, content in agent.chat(user_message):
            if msg_type == "text":
                full_response += content
        if full_response:
            _save_message(conv_id, "assistant", full_response, {"role": "assistant", "content": full_response})
            _update_conv_time(conv_id)
    except Exception as e:
        raise HTTPException(500, f"AI 响应出错: {e}")

    return {"response": full_response, "conversation_id": conv_id}


# ── 回退 API（编辑重发用：删除最后一条用户消息及其后的 AI 回复）──

@app.post("/api/conversations/{conv_id}/rewind")
async def rewind_conversation(conv_id: int, request: Request):
    user = require_user(request)
    db = get_db()
    conv = db.execute("SELECT id FROM conversations WHERE id=? AND user_id=?", (conv_id, user["id"])).fetchone()
    if not conv:
        db.close()
        raise HTTPException(404, "对话不存在")

    # 找到最后一条 user 消息的 id
    last_user = db.execute(
        "SELECT id FROM messages WHERE conversation_id=? AND role='user' ORDER BY id DESC LIMIT 1",
        (conv_id,)
    ).fetchone()
    if not last_user:
        db.close()
        return {"ok": True, "deleted": 0}

    # 删除该 user 消息及之后的所有消息
    deleted = db.execute(
        "DELETE FROM messages WHERE conversation_id=? AND id >= ?",
        (conv_id, last_user["id"])
    ).rowcount
    db.commit()
    db.close()

    # 清除缓存的 agent，下次自动重建
    _agents.pop(_agent_key(user["id"], conv_id), None)
    return {"ok": True, "deleted": deleted}


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
  max-width: 70%;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
}
.msg.user .msg-bubble {
  background: var(--user-msg);
  color: var(--user-msg-text);
  border-radius: 16px 16px 4px 16px;
  padding: 10px 18px;
  font-size: 14px;
  line-height: 1.65;
}
/* Action buttons below user messages */
.msg.user .msg-actions { display:flex; gap:2px; margin-top:2px; opacity:0; transition:opacity 0.15s; padding-right:4px; }
.msg.user:hover .msg-actions { opacity:1; }
.msg.user .msg-actions button { background:none; border:none; width:22px; height:22px; border-radius:4px; display:flex; align-items:center; justify-content:center; cursor:pointer; color:var(--ink-lighter); transition:all 0.12s; }
.msg.user .msg-actions button:hover { background:var(--border); color:var(--ink-light); }
.msg.user .msg-actions button svg { width:13px; height:13px; fill:currentColor; stroke:currentColor; stroke-width:0.5; }
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
  padding:12px 16px 16px;
}
.input-area .input-wrap {
  display:flex;
  align-items:flex-end;
  gap:8px;
  background:var(--bg);
  border:1px solid var(--border);
  border-radius:12px;
  padding:6px 6px 6px 16px;
  transition:border-color 0.15s, box-shadow 0.15s;
}
.input-area .input-wrap:focus-within { border-color:var(--accent); box-shadow:0 0 0 2px rgba(184,146,90,0.1); background:#fff; }
.input-area textarea {
  flex:1;
  border:none;
  padding:8px 0;
  font-size:14px;
  font-family:var(--font-body);
  resize:none;
  outline:none;
  min-height:64px;
  max-height:280px;
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
.input-area.loading button#sendBtn { display:none; }
.input-area.loading button#stopBtn { display:flex !important; background:#c33; color:#fff; border:none; border-radius:8px; width:36px; height:36px; display:flex; align-items:center; justify-content:center; cursor:pointer; flex-shrink:0; animation:stopPulse 1.5s ease-in-out infinite; }
.input-area.loading button#stopBtn:hover { background:#a00; animation:none; }
@keyframes stopPulse { 0%,100% { box-shadow:0 0 0 0 rgba(204,51,51,0.4); } 50% { box-shadow:0 0 0 6px rgba(204,51,51,0); } }

.msg.user.editing { align-self:flex-end; max-width:80%; background:var(--surface); border:1px solid var(--accent); border-radius:16px; padding:8px 12px; position:relative; }
.msg.user.editing textarea { width:100%; min-height:64px; border:none; background:transparent; font-size:14px; font-family:var(--font-body); resize:vertical; outline:none; color:var(--ink); line-height:1.6; padding:4px 0; }
.msg.user.editing .edit-actions { display:flex; gap:6px; margin-top:6px; justify-content:flex-end; }
.msg.user.editing .edit-actions button { padding:4px 14px; border-radius:6px; font-size:12px; font-family:inherit; cursor:pointer; border:none; transition:all 0.15s; }
.msg.user.editing .edit-actions .save-btn { background:var(--accent); color:#fff; }
.msg.user.editing .edit-actions .save-btn:hover { background:#a37d4a; }
.msg.user.editing .edit-actions .cancel-btn { background:var(--bg); color:var(--ink-light); border:1px solid var(--border); }
.msg.user.editing .edit-actions .cancel-btn:hover { border-color:var(--ink-lighter); }

@media (max-width:700px) {
  .sidebar { display:none; }
  .sidebar.open { display:flex; position:fixed; left:0; top:0; bottom:0; z-index:10; width:260px; box-shadow:4px 0 20px rgba(0,0,0,0.1); }
}
/* Sidebar toggle on desktop */
.sidebar.collapsed { display:none; }
.chat-layout.sidebar-collapsed .chat-main { max-width:100%; }
@keyframes toolPulse { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:0.6;transform:scale(0.85)} }
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
  <div class="chat-layout" id="chatLayout">
    <div class="sidebar" id="sidebar">
      <button class="new-btn" id="newConvBtn">+ 新对话</button>
      <div class="conv-list" id="convList"></div>
    </div>
    <div class="chat-main">
      <div class="messages" id="messages"></div>
      <div class="input-area" id="inputArea">
        <div class="input-wrap">
          <textarea id="input" rows="1" placeholder="输入你的问题…"></textarea>
          <button id="stopBtn" title="停止生成" style="display:none;"><svg viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor"/></svg></button>
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
let abortController = null;
let lastUserMsgEl = null;

const $ = id => document.getElementById(id);
const msgEl = $('messages');
const inputEl = $('input');
const sendBtn = $('sendBtn');
const stopBtn = $('stopBtn');
const inputArea = $('inputArea');

function restoreInput() {
  loading = false;
  abortController = null;
  inputArea.classList.remove('loading');
  sendBtn.disabled = false;
  inputEl.disabled = false;
  stopBtn.style.display = 'none';
  inputEl.focus();
}

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
      if (m.role === 'user') addMessage('user', escapeHtml(m.content), m.content);
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
function addMessage(role, content, rawText) {
  const div = document.createElement('div');
  div.className = 'msg ' + role;
  if (role === 'user') {
    div.dataset.rawText = rawText || content;
    div.innerHTML = '<div class="msg-bubble">' + content + '</div>';
    // Three action buttons
    const actions = document.createElement('div');
    actions.className = 'msg-actions';
    actions.innerHTML =
      '<button class="copy-btn" title="复制">' +
        '<svg viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2" stroke="currentColor" stroke-width="1.5" fill="none"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" stroke="currentColor" stroke-width="1.5" fill="none"/></svg>' +
      '</button>' +
      '<button class="regen-btn" title="重新生成">' +
        '<svg viewBox="0 0 24 24"><path d="M17.65 6.35A7.96 7.96 0 0 0 12 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08A5.99 5.99 0 0 1 12 18c-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z" fill="currentColor"/></svg>' +
      '</button>' +
      '<button class="edit-btn" title="编辑">' +
        '<svg viewBox="0 0 24 24"><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04a1 1 0 0 0 0-1.41l-2.34-2.34a1 1 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z" fill="currentColor"/></svg>' +
      '</button>';
    div.appendChild(actions);
  } else {
    div.innerHTML = content;
  }
  msgEl.appendChild(div);
  requestAnimationFrame(() => msgEl.scrollTop = msgEl.scrollHeight);

  // Bind action handlers
  if (role === 'user') {
    const raw = div.dataset.rawText;
    div.querySelector('.copy-btn').onclick = () => {
      navigator.clipboard.writeText(raw).catch(() => {});
    };
    div.querySelector('.regen-btn').onclick = () => regenerate(div);
    div.querySelector('.edit-btn').onclick = () => startEdit(div);
    if (rawText != null) lastUserMsgEl = div;
  }
  return div;
}

function startEdit(msgDiv) {
  const origText = msgDiv.dataset.rawText || '';
  msgDiv.className = 'msg user editing';
  msgDiv.innerHTML = '<textarea class="edit-textarea">' + escapeHtml(origText) + '</textarea>' +
    '<div class="edit-actions">' +
      '<button class="cancel-btn">取消</button>' +
      '<button class="save-btn">发送</button>' +
    '</div>';
  const ta = msgDiv.querySelector('textarea');
  const saveBtn = msgDiv.querySelector('.save-btn');
  const cancelBtn = msgDiv.querySelector('.cancel-btn');
  ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length);
  saveBtn.onclick = () => doEdit(msgDiv, ta.value.trim());
  cancelBtn.onclick = () => { restoreMsgActions(msgDiv, origText); };
  ta.onkeydown = (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); doEdit(msgDiv, ta.value.trim()); } };
}

function restoreMsgActions(msgDiv, text) {
  msgDiv.className = 'msg user';
  msgDiv.innerHTML = '<div class="msg-bubble">' + escapeHtml(text) + '</div>' +
    '<div class="msg-actions">' +
      '<button class="copy-btn" title="复制"><svg viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2" stroke="currentColor" stroke-width="1.5" fill="none"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" stroke="currentColor" stroke-width="1.5" fill="none"/></svg></button>' +
      '<button class="regen-btn" title="重新生成"><svg viewBox="0 0 24 24"><path d="M17.65 6.35A7.96 7.96 0 0 0 12 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08A5.99 5.99 0 0 1 12 18c-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z" fill="currentColor"/></svg></button>' +
      '<button class="edit-btn" title="编辑"><svg viewBox="0 0 24 24"><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04a1 1 0 0 0 0-1.41l-2.34-2.34a1 1 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z" fill="currentColor"/></svg></button>' +
    '</div>';
  msgDiv.querySelector('.copy-btn').onclick = () => navigator.clipboard.writeText(text).catch(() => {});
  msgDiv.querySelector('.regen-btn').onclick = () => regenerate(msgDiv);
  msgDiv.querySelector('.edit-btn').onclick = () => startEdit(msgDiv);
}

async function doEdit(msgDiv, newText) {
  if (!newText || !currentConvId) return;
  await fetch('/api/conversations/' + currentConvId + '/rewind', {method:'POST'});
  msgDiv.remove();
  const msgs = msgEl.querySelectorAll('.msg');
  const lastAi = msgs[msgs.length - 1];
  if (lastAi && lastAi.classList.contains('agent')) lastAi.remove();
  inputEl.value = newText;
  send();
}

async function regenerate(msgDiv) {
  if (!currentConvId) return;
  await fetch('/api/conversations/' + currentConvId + '/rewind', {method:'POST'});
  msgDiv.remove();
  const msgs = msgEl.querySelectorAll('.msg');
  const lastAi = msgs[msgs.length - 1];
  if (lastAi && lastAi.classList.contains('agent')) lastAi.remove();
  inputEl.value = msgDiv.dataset.rawText || '';
  send();
}

function escapeHtml(t) {
  const d = document.createElement('div');
  d.textContent = t;
  return d.innerHTML;
}

inputEl.oninput = () => {
  inputEl.style.height = 'auto';
  inputEl.style.height = Math.min(inputEl.scrollHeight, 280) + 'px';
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

  addMessage('user', escapeHtml(text), true);
  loading = true;
  inputArea.classList.add('loading');
  sendBtn.disabled = true;
  inputEl.disabled = true;
  stopBtn.style.display = '';

  const dots = '<div class="thinking-dots"><span></span><span></span><span></span></div>';
  const msgDiv = addMessage('agent', dots);
  let toolStatus = null;
  let content = '';
  let wasAborted = false;

  function showToolStatus(text) {
    if (!toolStatus) {
      toolStatus = document.createElement('div');
      toolStatus.style.cssText = 'font-size:12px;color:var(--ink-lighter);padding:4px 0;align-self:flex-start;animation:msgIn 0.2s ease;display:flex;align-items:center;gap:6px;';
      const bar = document.createElement('span');
      bar.className = 'tool-status-bar';
      bar.style.cssText = 'display:inline-block;width:12px;height:12px;border-radius:50%;background:var(--accent);animation:toolPulse 1.2s ease-in-out infinite;flex-shrink:0;';
      toolStatus.appendChild(bar);
      const span = document.createElement('span');
      span.className = 'tool-status-text';
      toolStatus.appendChild(span);
      msgEl.insertBefore(toolStatus, msgEl.lastElementChild.nextSibling || null);
    }
    const isRender = text.includes('render_page');
    toolStatus.querySelector('.tool-status-text').textContent = isRender ? '生成页面中...' : text;
    toolStatus.querySelector('.tool-status-bar').style.background = isRender ? 'var(--highlight,#c5862b)' : 'var(--accent)';
    requestAnimationFrame(() => msgEl.scrollTop = msgEl.scrollHeight);
  }
  function hideToolStatus() {
    if (toolStatus) { toolStatus.remove(); toolStatus = null; }
  }

  abortController = new AbortController();
  stopBtn.onclick = () => { abortController.abort(); };

  try {
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({message: text, conversation_id: currentConvId}),
      signal: abortController.signal,
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
          if (p.type === 'text') {
            hideToolStatus();
            content += p.content;
            msgDiv.innerHTML = marked.parse(content).replace(/<a\s+href=/g, '<a target="_blank" href=');
          } else if (p.type === 'tool_start') {
            showToolStatus('⏳ ' + escapeHtml(p.content));
          } else if (p.type === 'render_done') {
            hideToolStatus();
            const pageUrl = p.content;
            const notif = document.createElement('div');
            notif.style.cssText = 'background:var(--accent-bg,#f5f0e8);border:1px solid var(--accent);border-radius:8px;padding:10px 14px;margin:6px 0;align-self:flex-start;animation:msgIn 0.3s ease;font-size:13px;';
            notif.innerHTML = '📄 页面已生成：<a href="'+pageUrl+'" target="_blank" style="color:var(--accent);font-weight:600;text-decoration:underline;">点击打开</a>（已自动在新标签页打开）';
            msgEl.appendChild(notif);
            requestAnimationFrame(() => msgEl.scrollTop = msgEl.scrollHeight);
            window.open(pageUrl, '_blank');
          }
        } catch(e) {}
      }
      requestAnimationFrame(() => msgEl.scrollTop = msgEl.scrollHeight);
    }
  } catch(e) {
    if (e.name === 'AbortError') {
      wasAborted = true;
      hideToolStatus();
      if (content) {
        msgDiv.innerHTML = marked.parse(content + '\n\n> ⏸️ **已停止**').replace(/<a\s+href=/g, '<a target="_blank" href=');
      } else {
        msgDiv.innerHTML = '<p style="color:var(--ink-lighter);font-style:italic;font-size:13px;">⏸️ 已停止</p>';
      }
    } else {
      msgDiv.innerHTML = '<p style="color:#b33;">连接失败，请确认服务器正在运行</p>';
    }
  }

  restoreInput();
  if (!wasAborted) loadConvs();
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

// ── Sidebar toggle ──
$('menuBtn').onclick = () => {
  if (window.innerWidth <= 700) {
    $('sidebar').classList.toggle('open');
  } else {
    $('sidebar').classList.toggle('collapsed');
    $('chatLayout').classList.toggle('sidebar-collapsed');
  }
};

checkAuth();
</script>
</body>
</html>"""


@app.get("/")
async def index():
    return HTMLResponse(HTML, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


# ── 启动 ──

def main():
    port = int(os.getenv("PORT", "8080"))
    print(f"诸葛策 Web 服务 → http://localhost:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
