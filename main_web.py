#!/usr/bin/env python3
"""诸葛策 — Web 服务（用户系统 + 多对话）"""

import json
import os
import sys
import io
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

from db import get_db


def init_db():
    db = get_db()
    db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INT AUTO_INCREMENT PRIMARY KEY,
            username VARCHAR(255) UNIQUE NOT NULL,
            password_hash VARCHAR(255),
            wechat_openid VARCHAR(255) UNIQUE,
            created_at VARCHAR(50) NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            token VARCHAR(255) UNIQUE NOT NULL,
            created_at VARCHAR(50) NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            title VARCHAR(255) NOT NULL DEFAULT '新对话',
            created_at VARCHAR(50) NOT NULL,
            updated_at VARCHAR(50) NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INT AUTO_INCREMENT PRIMARY KEY,
            conversation_id INT NOT NULL,
            role VARCHAR(50) NOT NULL,
            content LONGTEXT,
            msg_json LONGTEXT NOT NULL,
            created_at VARCHAR(50) NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS profiles (
            user_id VARCHAR(255) PRIMARY KEY,
            data LONGTEXT NOT NULL,
            updated_at VARCHAR(50) NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS journal (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id VARCHAR(255) NOT NULL,
            entry_json LONGTEXT NOT NULL,
            saved_at VARCHAR(50) NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS decisions (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id VARCHAR(255) NOT NULL,
            entry_json LONGTEXT NOT NULL,
            saved_at VARCHAR(50) NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS user_questions (
            id INT AUTO_INCREMENT PRIMARY KEY,
            username VARCHAR(255) NOT NULL,
            question TEXT NOT NULL,
            answer_hash VARCHAR(255) NOT NULL
        )
    """)
    try:
        db.execute("ALTER TABLE users ADD COLUMN total_turns INT NOT NULL DEFAULT 0")
    except Exception:
        pass  # 列已存在则跳过
    for idx_def in ["idx_sessions_token ON sessions(token)",
                     "idx_conversations_user ON conversations(user_id)",
                     "idx_messages_conv ON messages(conversation_id)"]:
        try:
            db.execute(f"CREATE INDEX {idx_def}")
        except Exception:
            pass  # 索引已存在则跳过

    # default 用户数据保留（修复前遗留的引导数据），load_profile 会回退读取
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id INT AUTO_INCREMENT PRIMARY KEY,
                user_id INT,
                content TEXT NOT NULL,
                contact VARCHAR(255),
                created_at VARCHAR(50) NOT NULL
            )
        """)
    except Exception:
        pass

    db.execute("""
        CREATE TABLE IF NOT EXISTS bookmarks (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            conversation_id INT NOT NULL,
            message_id INT NOT NULL,
            role VARCHAR(10) NOT NULL,
            content_preview LONGTEXT NOT NULL,
            created_at VARCHAR(50) NOT NULL
        )
    """)
    try:
        db.execute("ALTER TABLE bookmarks MODIFY content_preview LONGTEXT NOT NULL")
    except Exception:
        pass
    # 清理指定测试/废弃用户
    for _del_user in ["shoubiao", "test"]:
        _du = db.execute("SELECT id FROM users WHERE username=%s", (_del_user,)).fetchone()
        if _du:
            _uid = _du["id"]
            db.execute("DELETE FROM messages WHERE conversation_id IN (SELECT id FROM conversations WHERE user_id=%s)", (_uid,))
            db.execute("DELETE FROM conversations WHERE user_id=%s", (_uid,))
            db.execute("DELETE FROM sessions WHERE user_id=%s", (_uid,))
            for _tbl in ["profiles", "journal", "decisions"]:
                db.execute(f"DELETE FROM {_tbl} WHERE user_id=%s", (_del_user,))
            db.execute("DELETE FROM user_questions WHERE username=%s", (_del_user,))
            db.execute("DELETE FROM users WHERE id=%s", (_uid,))

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
        "SELECT msg_json FROM messages WHERE conversation_id=%s ORDER BY id",
        (conv_id,)
    ).fetchall()
    db.close()
    agent.messages = []
    for row in rows:
        msg = json.loads(row["msg_json"])
        agent.messages.append(msg)


def _save_message(conv_id: int, role: str, content, msg_dict: dict, user_id: int = 0):
    db = get_db()
    cur = db.execute(
        "INSERT INTO messages (conversation_id, role, content, msg_json, created_at) VALUES (%s,%s,%s,%s,%s)",
        (conv_id, role, content or "", json.dumps(msg_dict, ensure_ascii=False), datetime.now().isoformat())
    )
    msg_id = cur.lastrowid
    if role == "assistant" and user_id:
        db.execute("UPDATE users SET total_turns = total_turns + 1 WHERE id=%s", (user_id,))
    db.commit()
    db.close()
    return msg_id


def _update_conv_time(conv_id: int):
    db = get_db()
    db.execute("UPDATE conversations SET updated_at=%s WHERE id=%s", (datetime.now().isoformat(), conv_id))
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
        "INSERT INTO sessions (user_id, token, created_at) VALUES (%s,%s,%s)",
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
        "SELECT u.id, u.username FROM sessions s JOIN users u ON s.user_id=u.id WHERE s.token=%s",
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
    security_questions = body.get("security_questions", [])
    if len(username) < 2 or len(password) < 4:
        raise HTTPException(400, "用户名至少2字符，密码至少4字符")
    db = get_db()
    existing = db.execute("SELECT id FROM users WHERE username=%s", (username,)).fetchone()
    if existing:
        db.close()
        raise HTTPException(400, "用户名已存在")
    db.execute(
        "INSERT INTO users (username, password_hash, created_at) VALUES (%s,%s,%s)",
        (username, hash_password(password), datetime.now().isoformat())
    )
    db.commit()
    user_id = db.execute("SELECT id FROM users WHERE username=%s", (username,)).fetchone()["id"]

    for q in security_questions:
        db.execute(
            "INSERT INTO user_questions (username, question, answer_hash) VALUES (%s,%s,%s)",
            (username, q["question"], hashlib.sha256(q["answer"].strip().lower().encode()).hexdigest())
        )
    db.commit()
    db.close()

    resp = Response(json.dumps({"ok": True}, ensure_ascii=False), media_type="application/json")
    create_session(resp, user_id)
    return resp


@app.post("/api/onboard")
async def onboard(request: Request):
    user = require_user(request)
    body = await request.json()
    name = body.get("name", "").strip()
    city = body.get("city", "").strip()
    focus = body.get("focus", "").strip()

    # 保存画像（先设置用户上下文，确保存到正确的用户下）
    from memory import set_user as _mu, save_profile
    _mu(user["username"])
    profile_data = {k: v for k, v in [("name", name), ("city", city), ("focus_area", focus)] if v}
    profile_data["onboarded_at"] = datetime.now().isoformat()
    save_profile(profile_data)

    # 创建首条对话
    now = datetime.now().isoformat()
    title = f"{name or '新用户'}的规划" if focus else "新对话"
    db = get_db()
    cur = db.execute(
        "INSERT INTO conversations (user_id, title, created_at, updated_at) VALUES (%s,%s,%s,%s)",
        (user["id"], title, now, now)
    )
    db.commit()
    conv_id = cur.lastrowid
    db.close()

    # 生成欢迎语
    if name and city:
        greeting = f"你好，在{city}的{name}！"
    elif name:
        greeting = f"你好，{name}！"
    elif city:
        greeting = f"你好，在{city}的朋友！"
    else:
        greeting = "你好！"

    welcome = (
        f"{greeting} 🙌\n\n"
        "我是**诸葛策**，你的专属人生军师。\n\n"
        "我能帮你做这些事：\n\n"
        "📋 **梳理现状** — 分析职业、财务、健康等各方面\n"
        "🎯 **规划目标** — 制定短期和长期计划\n"
        "🧠 **辅助决策** — 多角度分析，更明智的选择\n"
        "📝 **记录复盘** — 写日记、做决策、追踪进展"
    )
    if focus:
        welcome += f"\n\n你选择了 **{focus}** 方向，这正是我擅长的领域之一。我们可以从这里开始深入聊聊。"

    welcome += "\n\n有什么想问的，直接说就行 😊"

    from memory import set_user as _set_user
    _set_user(user["username"])
    _save_message(conv_id, "assistant", welcome, {"role": "assistant", "content": welcome}, user["id"])
    _update_conv_time(conv_id)

    return {"conv_id": conv_id, "welcome": welcome}


@app.get("/api/check-username")
async def check_username(username: str = ""):
    if len(username) < 2:
        return {"available": False}
    db = get_db()
    row = db.execute("SELECT id FROM users WHERE username=%s", (username,)).fetchone()
    db.close()
    return {"available": row is None}


@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "").strip()

    db = get_db()
    row = db.execute(
        "SELECT id, username FROM users WHERE username=%s AND password_hash=%s",
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
    row = db.execute("SELECT id, username FROM users WHERE wechat_openid=%s", (openid,)).fetchone()
    is_new = False
    if row:
        user_id = row["id"]
        username = row["username"]
    else:
        # 创建新用户（password_hash 设为空占位，因为表有 NOT NULL 约束）
        username = f"{_DEFAULT_USER_PREFIX}{openid[-8:]}"
        now = datetime.now().isoformat()
        cur = db.execute(
            "INSERT INTO users (username, password_hash, wechat_openid, created_at) VALUES (%s,%s,%s,%s)",
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
    row = db.execute("SELECT id FROM users WHERE username=%s", (username,)).fetchone()
    if row:
        user_id = row["id"]
    else:
        cur = db.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (%s,%s,%s)",
            (username, "", now)
        )
        db.commit()
        user_id = cur.lastrowid
    db.close()

    token = create_session(None, user_id)
    return {"token": token, "user_id": user_id, "username": username}


# ── 密保问题 API ──

SECURITY_QUESTIONS_POOL = [
    "你的小学名称是什么？",
    "你的初中名称是什么？",
    "你的高中名称是什么？",
    "你最喜欢的电影是什么？",
    "你最喜欢的书籍是什么？",
    "你最喜欢的动物是什么？",
    "你的出生城市是哪里？",
    "你母亲的姓氏是什么？",
    "你父亲的姓氏是什么？",
    "你的第一位班主任名字是什么？",
    "你最喜欢的食物是什么？",
    "你最想去旅游的国家是哪里？",
]


@app.get("/api/user-questions")
async def get_user_questions(username: str = ""):
    if not username:
        raise HTTPException(400, "请提供用户名")
    db = get_db()
    rows = db.execute(
        "SELECT question FROM user_questions WHERE username=%s ORDER BY id",
        (username,)
    ).fetchall()
    db.close()
    if not rows:
        raise HTTPException(404, "用户不存在或未设置密保问题")
    return {"questions": [r["question"] for r in rows]}


@app.post("/api/user-questions")
async def set_user_questions(request: Request):
    user = require_user(request)
    body = await request.json()
    questions = body.get("questions", [])
    if len(questions) < 2:
        raise HTTPException(400, "请设置至少2个密保问题")

    db = get_db()
    db.execute("DELETE FROM user_questions WHERE username=%s", (user["username"],))
    for q in questions:
        db.execute(
            "INSERT INTO user_questions (username, question, answer_hash) VALUES (%s,%s,%s)",
            (user["username"], q["question"], hashlib.sha256(q["answer"].strip().lower().encode()).hexdigest())
        )
    db.commit()
    db.close()
    return {"ok": True}


@app.post("/api/reset-password")
async def reset_password(request: Request):
    body = await request.json()
    username = body.get("username", "").strip()
    answers = body.get("answers", [])
    new_password = body.get("new_password", "").strip()

    if not username or not answers or len(new_password) < 4:
        raise HTTPException(400, "参数不完整")
    if len(answers) < 2:
        raise HTTPException(400, "请回答所有密保问题")

    db = get_db()
    rows = db.execute(
        "SELECT answer_hash FROM user_questions WHERE username=%s ORDER BY id",
        (username,)
    ).fetchall()
    if not rows:
        db.close()
        raise HTTPException(404, "用户不存在或未设置密保问题")

    for i, row in enumerate(rows):
        if i >= len(answers):
            break
        user_answer_hash = hashlib.sha256(answers[i]["answer"].strip().lower().encode()).hexdigest()
        if user_answer_hash != row["answer_hash"]:
            db.close()
            raise HTTPException(403, "密保问题答案错误")

    db.execute(
        "UPDATE users SET password_hash=%s WHERE username=%s",
        (hash_password(new_password), username)
    )
    db.commit()
    db.close()
    return {"ok": True}



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
        "SELECT id, title, created_at, updated_at FROM conversations WHERE user_id=%s ORDER BY updated_at DESC",
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
        "INSERT INTO conversations (user_id, title, created_at, updated_at) VALUES (%s,%s,%s,%s)",
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
    conv = db.execute("SELECT id FROM conversations WHERE id=%s AND user_id=%s", (conv_id, user["id"])).fetchone()
    if not conv:
        db.close()
        raise HTTPException(404, "对话不存在")
    db.execute("DELETE FROM messages WHERE conversation_id=%s", (conv_id,))
    db.execute("DELETE FROM conversations WHERE id=%s", (conv_id,))
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
    conv = db.execute("SELECT id FROM conversations WHERE id=%s AND user_id=%s", (conv_id, user["id"])).fetchone()
    if not conv:
        db.close()
        raise HTTPException(404, "对话不存在")
    rows = db.execute(
        "SELECT id, role, content FROM messages WHERE conversation_id=%s AND role IN ('user','assistant') AND content != '' ORDER BY id",
        (conv_id,)
    ).fetchall()
    db.close()
    result = []
    for r in rows:
        result.append({"role": r["role"], "content": r["content"], "id": r["id"]})
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

    import os as _os
    _max_turns = int(_os.environ.get("MAX_TURNS", "50"))

    # 检查用量限制（total_turns 不随删对话减少，确保历史全量计入）
    _user_turns = db.execute(
        "SELECT total_turns FROM users WHERE id=%s", (user["id"],)
    ).fetchone()
    if _user_turns and _user_turns["total_turns"] >= _max_turns:
        db.close()
        raise HTTPException(403, f"免费体验已达上限（{_max_turns} 轮），联系作者 roc9233 解锁")

    # 验证对话属于用户，或自动创建新对话
    if conv_id:
        conv = db.execute("SELECT id, title FROM conversations WHERE id=%s AND user_id=%s", (conv_id, user["id"])).fetchone()
        if not conv:
            db.close()
            raise HTTPException(404, "对话不存在")
        # 首条消息时自动更新标题
        if conv["title"] == "新对话":
            title = user_message[:30].strip()
            if len(title) > 0:
                db.execute("UPDATE conversations SET title=%s WHERE id=%s", (title, conv_id))
                db.commit()
    else:
        # 自动创建新对话
        now = datetime.now().isoformat()
        title = user_message[:30].strip() or "新对话"
        cur = db.execute(
            "INSERT INTO conversations (user_id, title, created_at, updated_at) VALUES (%s,%s,%s,%s)",
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
    user_msg_id = _save_message(conv_id, "user", user_message, {"role": "user", "content": user_message})
    _update_conv_time(conv_id)

    async def event_stream():
        full_response = ""
        # 发送用户消息 ID
        yield f"data: {json.dumps({'type': 'user_msg_id', 'message_id': user_msg_id}, ensure_ascii=False)}\n\n"
        try:
            for msg_type, content in agent.chat(user_message):
                if await request.is_disconnected():
                    break
                if msg_type == "text":
                    full_response += content
                data = json.dumps({"type": msg_type, "content": content}, ensure_ascii=False)
                yield f"data: {data}\n\n"

            # 保存 assistant 回复（同时累计轮数），在 [DONE] 之前
            assistant_msg_id = 0
            if full_response:
                assistant_msg_id = _save_message(conv_id, "assistant", full_response, {"role": "assistant", "content": full_response}, user["id"])
                _update_conv_time(conv_id)
                yield f"data: {json.dumps({'type': 'assistant_msg_id', 'message_id': assistant_msg_id}, ensure_ascii=False)}\n\n"

            yield "data: [DONE]\n\n"
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

    import os as _os
    _max_turns = int(_os.environ.get("MAX_TURNS", "50"))
    _user_turns = db.execute(
        "SELECT total_turns FROM users WHERE id=%s", (user["id"],)
    ).fetchone()
    if _user_turns and _user_turns["total_turns"] >= _max_turns:
        db.close()
        raise HTTPException(403, f"免费体验已达上限（{_max_turns} 轮），联系作者 roc9233 解锁")

    if conv_id:
        conv = db.execute("SELECT id, title FROM conversations WHERE id=%s AND user_id=%s", (conv_id, user["id"])).fetchone()
        if not conv:
            db.close()
            raise HTTPException(404, "对话不存在")
        if conv["title"] == "新对话":
            title = user_message[:30].strip()
            if len(title) > 0:
                db.execute("UPDATE conversations SET title=%s WHERE id=%s", (title, conv_id))
                db.commit()
    else:
        now = datetime.now().isoformat()
        title = user_message[:30].strip() or "新对话"
        cur = db.execute(
            "INSERT INTO conversations (user_id, title, created_at, updated_at) VALUES (%s,%s,%s,%s)",
            (user["id"], title, now, now)
        )
        db.commit()
        conv_id = cur.lastrowid

    db.close()

    from memory import set_user as _set_user
    _set_user(user["username"])
    agent = get_agent_for_conv(user["id"], conv_id)

    user_msg_id = _save_message(conv_id, "user", user_message, {"role": "user", "content": user_message})
    _update_conv_time(conv_id)

    full_response = ""
    assistant_msg_id = 0
    try:
        for msg_type, content in agent.chat(user_message):
            if msg_type == "text":
                full_response += content
        if full_response:
            assistant_msg_id = _save_message(conv_id, "assistant", full_response, {"role": "assistant", "content": full_response}, user["id"])
            _update_conv_time(conv_id)
    except Exception as e:
        raise HTTPException(500, f"AI 响应出错: {e}")

    return {"response": full_response, "conversation_id": conv_id, "user_msg_id": user_msg_id, "assistant_msg_id": assistant_msg_id}


# ── 回退 API（编辑重发用：删除最后一条用户消息及其后的 AI 回复）──

@app.post("/api/conversations/{conv_id}/rewind")
async def rewind_conversation(conv_id: int, request: Request):
    user = require_user(request)
    db = get_db()
    conv = db.execute("SELECT id FROM conversations WHERE id=%s AND user_id=%s", (conv_id, user["id"])).fetchone()
    if not conv:
        db.close()
        raise HTTPException(404, "对话不存在")

    # 找到最后一条 user 消息的 id
    last_user = db.execute(
        "SELECT id FROM messages WHERE conversation_id=%s AND role='user' ORDER BY id DESC LIMIT 1",
        (conv_id,)
    ).fetchone()
    if not last_user:
        db.close()
        return {"ok": True, "deleted": 0}

    # 删除该 user 消息及之后的所有消息
    deleted = db.execute(
        "DELETE FROM messages WHERE conversation_id=%s AND id >= %s",
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


# ── 职场仪表盘 ──

@app.get("/api/dashboard")
async def career_dashboard(request: Request):
    user = require_user(request)
    from memory import set_user, load_full_context, load_decisions
    set_user(user["username"])
    ctx = load_full_context()
    p = ctx.get("profile", {})
    basic = p.get("basic", {})
    domains = p.get("domains", {})

    # ── 当前职位 ──
    position = {
        "name": basic.get("name", ""),
        "title": basic.get("occupation", ""),
        "company": basic.get("company", ""),
        "level": basic.get("level", ""),
        "city": basic.get("city", ""),
        "identity": p.get("identity", ""),
    }

    # ── 核心技能（从 profile 各字段提取关键词）──
    skill_keywords = set()
    career_info = domains.get("career", {})
    for v in career_info.values():
        if isinstance(v, str):
            for kw in ["产品", "AI", "数据", "全栈", "金融", "管理", "设计", "技术", "创作", "运营", "市场", "增长"]:
                if kw in v:
                    skill_keywords.add(kw)
        elif isinstance(v, dict):
            for sv in v.values():
                if isinstance(sv, str):
                    for kw in ["产品", "AI", "数据", "全栈", "金融", "管理", "设计", "技术", "创作", "运营", "市场", "增长"]:
                        if kw in sv:
                            skill_keywords.add(kw)
    # 从身份定义提取
    for field in ["identity", "self_definition", "previous_definition"]:
        val = p.get(field, "")
        if isinstance(val, str):
            for kw in ["产品", "AI", "数据", "全栈", "金融", "管理", "设计", "技术", "创作", "运营", "市场", "增长"]:
                if kw in val:
                    skill_keywords.add(kw)
    skills = sorted(skill_keywords) if skill_keywords else ["待完善"]

    # ── 职业阶段（根据 level 和年龄粗略推断）──
    level = str(basic.get("level", "")).lower()
    age = basic.get("age", 0)
    if any(l in level for l in ["p8", "p9", "p10", "总监", "vp", "director"]):
        stage_label, stage_progress = "成熟期", 80
    elif any(l in level for l in ["p6", "p7", "高级", "senior", "专家", "资深"]):
        stage_label, stage_progress = "成长期", 55
    elif any(l in level for l in ["p5", "p4", "中级", "初级", "entry"]):
        stage_label, stage_progress = "筑基期", 35
    elif age and age <= 28:
        stage_label, stage_progress = "探索期", 20
    else:
        stage_label, stage_progress = "成长期", 50

    # 从 career_info 中找更精确的阶段
    for v in career_info.values():
        if isinstance(v, str):
            if "转行" in v or "转型" in v:
                stage_label, stage_progress = "转型期", 40
                break
            if "创业" in v or "副业" in v:
                stage_progress = min(stage_progress + 10, 90)

    # ── 活跃目标 ──
    known_goals = []
    # 从 career_info 提取
    if career_info.get("xiaohongshu_progress"):
        known_goals.append({"title": "小红书内容输出", "status": "active"})
    products = career_info.get("products", [])
    for prod in products if isinstance(products, list) else [products] if isinstance(products, dict) else []:
        if isinstance(prod, dict) and prod.get("description"):
            known_goals.append({"title": prod["description"][:30], "status": "active"})
    # 从 mission/values 推断
    if p.get("mission"):
        known_goals.append({"title": "践行人生使命", "status": "active"})
    goals = known_goals[:4] if known_goals else [{"title": "完善职业档案", "status": "pending"}]

    # ── 关联维度 ──
    dim_map = {
        "finance": {"label": "财务", "key": "finance"},
        "family": {"label": "家庭", "key": "family"},
        "health": {"label": "健康", "key": "health"},
        "learning": {"label": "学习", "key": "learning"},
        "social": {"label": "社交", "key": "social"},
        "spiritual": {"label": "精神", "key": "spiritual"},
    }
    dimensions = {}
    for dim_id, dim_info in dim_map.items():
        dim_data = domains.get(dim_id, {}) or p.get(dim_id, {})
        if isinstance(dim_data, dict) and any(v for v in dim_data.values()):
            dimensions[dim_id] = {"label": dim_info["label"], "status": "good"}
        else:
            dimensions[dim_id] = {"label": dim_info["label"], "status": "unknown"}

    # ── 近期里程碑 ──
    decisions = load_decisions(5)
    milestones = []
    for d in decisions:
        title = d.get("decision", "")[:40]
        if title:
            milestones.append({
                "title": title,
                "type": "decision",
                "date": d.get("_saved_at", "")[:10],
            })
    # 检查 career_info 中的里程碑
    if career_info.get("identity_detail"):
        milestones.insert(0, {"title": "职业身份定位完成", "type": "achievement"})

    return {
        "position": position,
        "stage": {"label": stage_label, "progress": stage_progress},
        "skills": skills,
        "goals": goals,
        "dimensions": dimensions,
        "milestones": milestones[:5],
    }


# ── 反馈 ──

@app.post("/api/feedback")
async def submit_feedback(request: Request):
    user = require_user(request)
    body = await request.json()
    content = (body.get("content") or "").strip()
    contact = (body.get("contact") or "").strip()
    if not content:
        raise HTTPException(400, "请填写反馈内容")
    db = get_db()
    db.execute(
        "INSERT INTO feedback (user_id, content, contact, created_at) VALUES (%s,%s,%s,%s)",
        (user["id"], content, contact or None, datetime.now().isoformat())
    )
    db.commit()
    db.close()
    return {"ok": True}


# ── 收藏 ──

@app.post("/api/bookmarks")
async def create_bookmark(request: Request):
    user = require_user(request)
    body = await request.json()
    conversation_id = body.get("conversation_id")
    message_id = body.get("message_id")
    role = body.get("role")
    content_preview = body.get("content_preview") or ""
    if not all([conversation_id, message_id, role]):
        raise HTTPException(400, "缺少必要字段")
    db = get_db()
    existing = db.execute(
        "SELECT id FROM bookmarks WHERE user_id=%s AND message_id=%s",
        (user["id"], message_id)
    ).fetchone()
    if existing:
        db.close()
        return {"ok": True, "id": existing["id"], "created": False}
    db.execute(
        "INSERT INTO bookmarks (user_id, conversation_id, message_id, role, content_preview, created_at) VALUES (%s,%s,%s,%s,%s,%s)",
        (user["id"], conversation_id, message_id, role, content_preview, datetime.now().isoformat())
    )
    db.commit()
    new_id = db.execute("SELECT LAST_INSERT_ID() as id").fetchone()["id"]
    db.close()
    return {"ok": True, "id": new_id, "created": True}


@app.get("/api/bookmarks")
async def list_bookmarks(request: Request):
    user = require_user(request)
    page = max(1, int(request.query_params.get("page", 1)))
    page_size = max(1, min(100, int(request.query_params.get("page_size", 10))))
    offset = (page - 1) * page_size
    db = get_db()
    total = db.execute(
        "SELECT COUNT(*) AS cnt FROM bookmarks WHERE user_id=%s",
        (user["id"],)
    ).fetchone()["cnt"]
    rows = db.execute("""
        SELECT b.id, b.conversation_id, b.message_id, b.role, b.content_preview, b.created_at,
               c.id IS NOT NULL AS conv_exists
        FROM bookmarks b
        LEFT JOIN conversations c ON b.conversation_id = c.id
        WHERE b.user_id = %s
        ORDER BY b.created_at DESC
        LIMIT %s OFFSET %s
    """, (user["id"], page_size, offset)).fetchall()
    db.close()
    return {"items": rows, "total": total, "page": page, "page_size": page_size}


@app.delete("/api/bookmarks/{bookmark_id}")
async def delete_bookmark(bookmark_id: int, request: Request):
    user = require_user(request)
    db = get_db()
    row = db.execute(
        "SELECT id FROM bookmarks WHERE id=%s AND user_id=%s",
        (bookmark_id, user["id"])
    ).fetchone()
    if not row:
        db.close()
        raise HTTPException(404, "收藏不存在")
    db.execute("DELETE FROM bookmarks WHERE id=%s", (bookmark_id,))
    db.commit()
    db.close()
    return {"ok": True}


# ── 静态页面 ──

HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>诸葛策 — 个人战略引擎</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 -18 100 85' fill='none' stroke='%238b7355' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M22 52 L22 28 Q22 -4 50 -10 Q78 -4 78 28 L78 52'/%3E%3Cpath d='M18 52 Q50 58 82 52' stroke-width='4'/%3E%3Cpath d='M18 48 Q50 54 82 48' stroke-width='1.8'/%3E%3Cpath d='M30 -6 L28 50' stroke-width='1.6' opacity='0.5'/%3E%3Cpath d='M38 -9 L36 51' stroke-width='1.6' opacity='0.5'/%3E%3Cpath d='M46 -10 L44 52' stroke-width='1.6' opacity='0.55'/%3E%3Cpath d='M54 -10 L56 52' stroke-width='1.6' opacity='0.55'/%3E%3Cpath d='M62 -9 L64 51' stroke-width='1.6' opacity='0.5'/%3E%3Cpath d='M70 -6 L72 50' stroke-width='1.6' opacity='0.5'/%3E%3Crect x='47' y='52' width='6' height='5' rx='1.5' fill='%238b7355' stroke='none'/%3E%3C/svg%3E">
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
  /* 登录专属 */
  --login-accent: #4a4240;
  --login-border: #d4cbc2;
  --login-bg: #f2efea;
  /* 注册专属 */
  --register-accent: #c8954e;
  --register-glow: rgba(200,149,78,0.12);
  --register-bg: #fcf7f0;
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
  position: relative;
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
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-family: var(--font-heading);
  font-size: 20px;
  margin-bottom: 16px;
  transition: all 0.4s ease;
}

/* ── Login 沉稳雅致 ── */
.auth-card.login-mode .seal {
  border: 2px solid var(--login-accent);
  color: var(--login-accent);
  background: transparent;
}
.auth-card.login-mode {
  background: var(--bg);
  border-color: var(--login-border);
  box-shadow: 0 2px 16px rgba(58,53,48,0.04);
}
.auth-card.login-mode input {
  background: #fff;
  border-color: var(--login-border);
}
.auth-card.login-mode input:focus {
  border-color: var(--login-accent);
  box-shadow: 0 0 0 3px rgba(74,66,64,0.06);
}
.auth-card.login-mode button {
  background: var(--login-accent);
  letter-spacing: 0.15em;
  font-weight: 400;
}
.auth-card.login-mode button:hover {
  background: #35302e;
}
.auth-card.login-mode h1 {
  letter-spacing: 0.2em;
  color: var(--login-accent);
}

/* ── Register 温暖新生 ── */
.auth-card.register-mode .seal {
  border: 2px solid var(--register-accent);
  color: #fff;
  background: linear-gradient(135deg, var(--register-accent), #dba35e);
  box-shadow: 0 4px 16px var(--register-glow);
}
.auth-card.register-mode {
  background: var(--register-bg);
  border-color: #e8d5b8;
  box-shadow: 0 4px 32px rgba(200,149,78,0.08);
}
.auth-card.register-mode::before {
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 3px;
  background: linear-gradient(90deg, var(--register-accent), #e8c48a, var(--register-accent));
  border-radius: 16px 16px 0 0;
}
.auth-card.register-mode {
  position: relative;
  overflow: hidden;
}
.auth-card.register-mode input {
  background: #fff;
  border-color: #e8d5b8;
}
.auth-card.register-mode input:focus {
  border-color: var(--register-accent);
  background: #fffefc;
  box-shadow: 0 0 0 3px var(--register-glow);
}
.auth-card.register-mode button {
  background: linear-gradient(135deg, var(--register-accent), #dba35e);
  letter-spacing: 0.08em;
  font-weight: 500;
}
.auth-card.register-mode button:hover {
  background: linear-gradient(135deg, #b88342, #cf9249);
  transform: translateY(-1px);
  box-shadow: 0 4px 12px var(--register-glow);
}
.auth-card.register-mode button {
  transition: all 0.25s ease;
}
.auth-card.register-mode .sub {
  color: #b8925a;
}
.auth-card.register-mode .toggle {
  color: var(--register-accent);
}
.auth-card.register-mode .toggle:hover {
  color: #a07940;
}

/* ── 首页 战情室风格 ── */
.app.visible {
  background: var(--bg);
  background-image:
    radial-gradient(ellipse at 0% 50%, rgba(184,146,90,0.03) 0%, transparent 50%),
    radial-gradient(ellipse at 100% 50%, rgba(58,53,48,0.02) 0%, transparent 50%);
}

/* ── Dashboard layout ── */
.app { display: none; height: 100vh; flex-direction: column; }
.app.visible { display: flex; }

.top-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 20px;
  background: linear-gradient(180deg, var(--surface) 0%, #faf8f4 100%);
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.top-bar-right {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 12px;
}
.top-bar h1 {
  font-family: var(--font-heading);
  font-size: 16px;
  font-weight: 600;
  letter-spacing: 0.15em;
  display: flex;
  align-items: center;
  gap: 8px;
}
.top-bar h1::before {
  display: none;
}
.top-bar .insight-btn {
  background: none;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 12px;
  font-size: 13px;
  font-weight: 500;
  color: var(--ink-light);
  cursor: pointer;
  letter-spacing: 0.04em;
  transition: all 0.15s;
  font-family: inherit;
  display: flex;
  align-items: center;
  gap: 4px;
}
.top-bar .insight-btn:hover {
  border-color: var(--accent);
  color: var(--accent);
}

.top-bar .bookmark-btn {
  background: none;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 12px;
  font-size: 13px;
  font-weight: 500;
  color: var(--ink-light);
  cursor: pointer;
  letter-spacing: 0.04em;
  transition: all 0.15s;
  font-family: inherit;
  display: flex;
  align-items: center;
  gap: 4px;
}
.top-bar .bookmark-btn:hover {
  border-color: var(--accent);
  color: var(--accent);
  background: var(--accent-light);
}
.top-bar .user-info {
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
.sidebar .sidebar-footer {
  padding: 8px 12px;
  border-top: 1px solid var(--border-light);
}
.sidebar .fb-btn {
  width: 100%;
  padding: 8px;
  background: none;
  border: 1px solid var(--border-light);
  border-radius: 8px;
  font-size: 12px;
  color: var(--ink-lighter);
  cursor: pointer;
  font-family: inherit;
  transition: all 0.15s;
}
.sidebar .fb-btn:hover {
  border-color: var(--accent);
  color: var(--accent);
}

/* ── 洞察模态框 ── */
.insight-overlay {
  display: none;
  position: fixed;
  inset: 0;
  z-index: 100;
  background: rgba(0,0,0,0.3);
  align-items: center;
  justify-content: center;
}
.insight-overlay.open { display: flex; }
.insight-overlay .insight-card {
  background: var(--bg);
  border-radius: 16px;
  width: 90%;
  max-width: 680px;
  max-height: 85vh;
  overflow-y: auto;
  position: relative;
  padding: 40px 32px 32px;
  box-shadow: 0 20px 60px rgba(0,0,0,0.15);
}
.insight-overlay .insight-close {
  position: absolute;
  top: 12px;
  right: 12px;
  width: 32px;
  height: 32px;
  border-radius: 50%;
  border: none;
  background: rgba(0,0,0,0.05);
  color: var(--ink-lighter);
  font-size: 16px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 0.15s;
}
.insight-overlay .insight-close:hover {
  background: rgba(0,0,0,0.1);
  color: var(--ink);
}
.sp-body {
  max-width: 640px;
  margin: 0 auto;
}
.sp-loading { text-align: center; padding: 60px 0; }
.sp-loading .sp-loading-text { font-size: 15px; color: var(--ink-light); margin-bottom: 20px; }
.sp-loading .sp-loading-bar-wrap {
  width: 200px; height: 4px; background: var(--border-light); border-radius: 4px;
  margin: 0 auto; overflow: hidden;
}
.sp-loading .sp-loading-bar {
  height: 100%; width: 0; background: var(--accent); border-radius: 4px;
  animation: insightProgress 2s ease forwards;
}
@keyframes insightProgress {
  from { width: 0; }
  to { width: 100%; }
}

/* ── 收藏模态框 ── */
.bm-overlay {
  display: none;
  position: fixed;
  inset: 0;
  z-index: 100;
  background: rgba(0,0,0,0.3);
  align-items: center;
  justify-content: center;
}
.bm-overlay.open { display: flex; }
.bm-overlay .bm-card {
  background: var(--bg);
  border-radius: 16px;
  width: 90%;
  max-width: 580px;
  max-height: 85vh;
  overflow-y: auto;
  position: relative;
  padding: 40px 28px 28px;
  box-shadow: 0 20px 60px rgba(0,0,0,0.15);
}
.bm-overlay .bm-close {
  position: absolute;
  top: 12px; right: 12px;
  width: 32px; height: 32px;
  border-radius: 50%;
  border: none;
  background: rgba(0,0,0,0.05);
  color: var(--ink-lighter);
  font-size: 16px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 0.15s;
}
.bm-overlay .bm-close:hover {
  background: rgba(0,0,0,0.1);
  color: var(--ink);
}
.bm-overlay h2 {
  font-family: var(--font-heading);
  font-size: 18px;
  color: var(--ink);
  margin: 0 0 16px;
  letter-spacing: 0.08em;
}
.bm-overlay .bm-empty {
  text-align: center;
  padding: 40px 0;
  color: var(--ink-lighter);
  font-size: 14px;
}
.bm-overlay .bm-list { min-height: 60px; }
.bm-overlay .bm-item {
  padding: 12px 14px;
  border: 1px solid var(--border-light);
  border-radius: 10px;
  margin-bottom: 10px;
  transition: background 0.12s, border-color 0.12s;
}
.bm-overlay .bm-item:last-child { margin-bottom: 0; }
.bm-overlay .bm-item:hover {
  background: var(--surface);
  border-color: var(--accent-light);
  cursor: pointer;
}
.bm-overlay .bm-item-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
}
.bm-overlay .bm-item-role {
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 4px;
  letter-spacing: 0.04em;
}
.bm-overlay .bm-item-role.user {
  background: var(--ink);
  color: #fff;
}
.bm-overlay .bm-item-role.assistant {
  background: var(--accent-light);
  color: var(--accent);
}
.bm-overlay .bm-item-del {
  background: none;
  border: none;
  font-size: 14px;
  color: var(--ink-lighter);
  cursor: pointer;
  padding: 2px 6px;
  border-radius: 4px;
  transition: all 0.12s;
}
.bm-overlay .bm-item-del:hover {
  background: rgba(200,50,50,0.1);
  color: #c33;
}
.bm-overlay .bm-item-jump {
  background: none;
  border: none;
  cursor: pointer;
  font-size: 12px;
  color: var(--accent);
  padding: 0 6px;
  margin-right: auto;
  transition: transform 0.12s;
}
.bm-overlay .bm-item-jump:hover {
  transform: translateX(2px);
}
.bm-overlay .bm-item-deleted-tag {
  color: var(--ink-light);
  font-size: 11px;
  margin-right: auto;
}
.bm-overlay .bm-item.conv-deleted {
  opacity: 0.75;
}
.bm-pagination {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  padding: 16px 0 4px;
  border-top: 1px solid var(--border-light);
  margin-top: 8px;
}
.bm-pg-info {
  font-size: 12px;
  color: var(--ink-lighter);
}
.bm-pg-pages {
  display: flex;
  align-items: center;
  gap: 6px;
}
.bm-pg-btn {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 4px 10px;
  cursor: pointer;
  font-size: 13px;
  color: var(--ink);
  transition: all 0.12s;
}
.bm-pg-btn:hover {
  border-color: var(--accent);
  color: var(--accent);
}
.bm-pg-current {
  font-size: 12px;
  color: var(--ink-light);
  padding: 0 4px;
}
.bm-pg-size select {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 4px 6px;
  font-size: 11px;
  color: var(--ink-light);
  cursor: pointer;
}
.bm-overlay .bm-item-preview {
  font-size: 13px;
  line-height: 1.55;
  color: var(--ink);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  margin-bottom: 6px;
  word-break: break-all;
  transition: all 0.2s;
  cursor: pointer;
}
.bm-overlay .bm-item.expanded .bm-item-preview {
  display: none;
}
.bm-overlay .bm-item-full {
  display: none;
  font-size: 13px;
  line-height: 1.7;
  color: var(--ink);
  margin-bottom: 8px;
  word-break: break-word;
  cursor: default;
}
.bm-overlay .bm-item-full p { margin: 0 0 8px; }
.bm-overlay .bm-item-full p:last-child { margin-bottom: 0; }
.bm-overlay .bm-item.expanded .bm-item-full {
  display: block;
}
.bm-overlay .bm-item.expanded {
  background: var(--surface);
  border-color: var(--accent-light);
}
.bm-overlay .bm-item-meta {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 11px;
  color: var(--ink-lighter);
}
.bm-overlay .bm-item-date {
  flex-shrink: 0;
  font-size: 11px;
}

/* 策论卡片 */
.sp-card {
  background: var(--surface);
  border: 1px solid var(--border-light);
  border-radius: 12px;
  padding: 24px 28px;
  margin-bottom: 16px;
}
.sp-card h3 {
  font-size: 13px;
  font-weight: 600;
  color: var(--ink-lighter);
  letter-spacing: 0.06em;
  margin: 0 0 14px;
  text-transform: uppercase;
}

/* 职位卡 */
.sp-pos-row {
  display: flex;
  align-items: center;
  gap: 16px;
}
.sp-avatar {
  width: 48px; height: 48px;
  border-radius: 50%;
  background: var(--accent);
  color: #fff;
  display: flex; align-items: center; justify-content: center;
  font-size: 20px; font-weight: 600;
  flex-shrink: 0;
}
.sp-pos-info { min-width: 0; }
.sp-pos-name { font-size: 18px; font-weight: 600; color: var(--ink); }
.sp-pos-title { font-size: 14px; color: var(--ink-light); margin-top: 2px; }
.sp-pos-identity { font-size: 13px; color: var(--accent); margin-top: 4px; }

/* 阶段 */
.sp-stage-row {
  display: flex;
  align-items: center;
  gap: 12px;
}
.sp-stage-label { font-size: 14px; color: var(--ink-light); white-space: nowrap; }
.sp-stage-bar {
  flex: 1; height: 6px;
  background: var(--border-light);
  border-radius: 6px;
  overflow: hidden;
}
.sp-stage-fill {
  height: 100%;
  background: linear-gradient(90deg, var(--accent), #c9a96e);
  border-radius: 6px;
  transition: width 0.8s ease;
}
.sp-stage-pct { font-size: 13px; color: var(--ink-lighter); width: 36px; text-align: right; }

/* 技能 */
.sp-skills {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.sp-skill-tag {
  padding: 4px 12px;
  background: var(--accent-light);
  color: var(--accent);
  border-radius: 6px;
  font-size: 13px;
  line-height: 1.5;
}

/* 目标 */
.sp-goal-list { list-style: none; padding: 0; margin: 0; }
.sp-goal-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 0;
  font-size: 14px;
  color: var(--ink);
  border-bottom: 1px solid var(--border-light);
}
.sp-goal-item:last-child { border-bottom: none; }
.sp-goal-dot {
  width: 7px; height: 7px;
  border-radius: 50%;
  flex-shrink: 0;
}
.sp-goal-dot.active { background: #4caf50; }
.sp-goal-dot.pending { background: var(--ink-lighter); }

/* 维度网格 */
.sp-dim-grid {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 8px;
}
.sp-dim-item {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px solid var(--border-light);
  font-size: 13px;
  color: var(--ink-light);
}
.sp-dim-dot {
  width: 8px; height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.sp-dim-dot.good { background: #4caf50; }
.sp-dim-dot.unknown { background: var(--ink-lighter); box-shadow: inset 0 0 0 1px rgba(0,0,0,0.1); }

/* 里程碑 */
.sp-ms-list { margin: 0; }
.sp-ms-item {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 8px 0;
  border-bottom: 1px solid var(--border-light);
  font-size: 13px;
  color: var(--ink);
  line-height: 1.4;
}
.sp-ms-item:last-child { border-bottom: none; }
.sp-ms-icon { font-size: 14px; margin-top: 1px; flex-shrink: 0; }
.sp-ms-text { flex: 1; min-width: 0; }
.sp-ms-date { color: var(--ink-lighter); font-size: 12px; white-space: nowrap; margin-left: 8px; }

@media (max-width: 700px) {
  .sp-body { padding: 20px 16px 60px; }
  .sp-card { padding: 18px; }
  .sp-dim-grid { grid-template-columns: 1fr 1fr; }
}

/* 反馈模态框 */
.fb-overlay {
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(58,53,48,0.3);
  backdrop-filter: blur(4px);
  z-index: 200;
  align-items: center;
  justify-content: center;
}
.fb-overlay.open { display: flex; }
.fb-overlay .fb-card {
  background: #fff;
  border-radius: 16px;
  padding: 32px;
  width: 400px;
  max-width: 90vw;
  position: relative;
  box-shadow: 0 12px 40px rgba(0,0,0,0.12);
  animation: fadeIn 0.2s ease;
}
.fb-overlay .fb-close {
  position: absolute;
  top: 12px; right: 16px;
  background: none;
  border: none;
  font-size: 20px;
  color: var(--ink-lighter);
  cursor: pointer;
  font-family: inherit;
}
.fb-overlay h2 {
  font-family: var(--font-heading);
  font-size: 20px;
  color: var(--ink);
  margin: 0 0 4px;
}
.fb-overlay .fb-sub {
  font-size: 13px;
  color: var(--ink-lighter);
  margin: 0 0 20px;
}
.fb-overlay textarea {
  width: 100%;
  padding: 12px;
  border: 1.5px solid var(--border);
  border-radius: 10px;
  font-size: 14px;
  font-family: inherit;
  resize: vertical;
  outline: none;
  box-sizing: border-box;
  transition: border-color 0.2s;
}
.fb-overlay textarea:focus {
  border-color: var(--accent);
}
.fb-overlay input {
  width: 100%;
  padding: 10px 12px;
  border: 1.5px solid var(--border);
  border-radius: 10px;
  font-size: 13px;
  font-family: inherit;
  outline: none;
  margin-top: 10px;
  box-sizing: border-box;
  transition: border-color 0.2s;
}
.fb-overlay input:focus {
  border-color: var(--accent);
}
.fb-overlay #fbSubmit {
  display: block;
  width: 100%;
  margin-top: 14px;
  padding: 12px;
  background: var(--ink);
  color: #fff;
  border: none;
  border-radius: 10px;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  font-family: inherit;
  transition: opacity 0.2s;
}
.fb-overlay #fbSubmit:hover { opacity: 0.85; }
.fb-overlay #fbSubmit:disabled { opacity: 0.4; cursor: default; }
.fb-overlay .fb-success {
  text-align: center;
  padding: 24px 0;
}
.fb-overlay .fb-success-icon {
  width: 48px; height: 48px;
  border-radius: 50%;
  background: #4caf50;
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 24px;
  margin: 0 auto 12px;
}
.fb-overlay .fb-success p {
  font-size: 15px;
  color: var(--ink-light);
  margin: 0;
}

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
.msg .msg-actions { display:flex; gap:2px; margin-top:2px; opacity:0; transition:opacity 0.15s; }
.msg:hover .msg-actions { opacity:1; }
.msg.user .msg-actions { padding-right:4px; }
.msg .msg-actions button { background:none; border:none; width:22px; height:22px; border-radius:4px; display:flex; align-items:center; justify-content:center; cursor:pointer; color:var(--ink-lighter); transition:all 0.12s; }
.msg .msg-actions button:hover { background:var(--border); color:var(--ink-light); }
.msg.user .msg-actions button svg { width:13px; height:13px; fill:currentColor; stroke:currentColor; stroke-width:0.5; }
.msg.agent .msg-actions button svg { width:13px; height:13px; }
.msg .msg-actions .bm-btn.bookmarked { color:var(--accent); }
.msg .msg-actions .bm-btn.bookmarked svg { fill:var(--accent); stroke:var(--accent); }
.msg.agent {
  align-self: flex-start;
  display: flex;
  flex-direction: column;
}
.msg-agent-wrap {
  background: var(--surface);
  border-radius: 16px 16px 16px 4px;
  padding: 16px 22px;
  font-size: 14px;
  line-height: 1.85;
  box-shadow: 0 1px 4px var(--shadow);
  border: 1px solid var(--border-light);
  color: var(--ink);
}
.msg.agent .msg-actions { padding-top: 4px; }
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

/* Welcome / Landing */
.welcome {
  max-width: 520px;
  margin: 40px auto 20px;
  padding: 0 20px;
  text-align: center;
  animation: welcomeFade 0.6s ease;
}
@keyframes welcomeFade {
  from { opacity: 0; transform: translateY(12px); }
  to { opacity: 1; transform: translateY(0); }
}

.welcome .hero-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  font-weight: 500;
  letter-spacing: 0.15em;
  color: var(--accent);
  padding: 4px 14px;
  border: 1px solid var(--accent-light);
  border-radius: 20px;
  margin-bottom: 20px;
  animation: welcomeFade 0.6s ease both;
  animation-delay: 0.1s;
}
.welcome .hero-title {
  font-family: var(--font-heading);
  font-size: 28px;
  font-weight: 700;
  color: var(--ink);
  letter-spacing: 0.08em;
  line-height: 1.3;
  margin-bottom: 10px;
  animation: welcomeFade 0.6s ease both;
  animation-delay: 0.2s;
}
.welcome .hero-title em {
  font-style: normal;
  color: var(--accent);
}
.welcome .hero-desc {
  font-size: 14px;
  color: var(--ink-light);
  line-height: 1.7;
  letter-spacing: 0.03em;
  margin-bottom: 32px;
  animation: welcomeFade 0.6s ease both;
  animation-delay: 0.3s;
}

/* Feature cards */
.welcome .features {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
  margin-bottom: 32px;
  text-align: left;
  animation: welcomeFade 0.6s ease both;
  animation-delay: 0.4s;
}
.welcome .feature-card {
  background: var(--surface);
  border: 1px solid var(--border-light);
  border-radius: 10px;
  padding: 14px 14px 14px 16px;
  cursor: default;
  transition: all 0.2s ease;
}
.welcome .feature-card:hover {
  border-color: var(--accent-light);
  box-shadow: 0 2px 12px rgba(184,146,90,0.06);
  transform: translateY(-2px);
}
.welcome .feature-card .fc-icon {
  width: 28px;
  height: 28px;
  border-radius: 6px;
  display: flex;
  align-items: center;
  justify-content: center;
  margin-bottom: 8px;
  font-size: 14px;
}
.welcome .feature-card .fc-icon svg { width: 16px; height: 16px; }
.welcome .feature-card .fc-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--ink);
  margin-bottom: 3px;
  letter-spacing: 0.03em;
}
.welcome .feature-card .fc-desc {
  font-size: 12px;
  color: var(--ink-lighter);
  line-height: 1.5;
}

/* Prompt chips */
.welcome .prompts {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 8px;
  animation: welcomeFade 0.6s ease both;
  animation-delay: 0.5s;
}
.welcome .prompt-chip {
  font-size: 12px;
  color: var(--ink-light);
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 20px;
  padding: 6px 16px;
  cursor: pointer;
  transition: all 0.2s ease;
  font-family: inherit;
}
.welcome .prompt-chip:hover {
  border-color: var(--accent);
  color: var(--accent);
  background: #fffcf8;
  box-shadow: 0 1px 8px rgba(184,146,90,0.1);
}
.welcome .prompts-label {
  width: 100%;
  font-size: 11px;
  color: var(--ink-lighter);
  letter-spacing: 0.08em;
  margin-bottom: 2px;
  opacity: 0.6;
}

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
.chat-layout.sidebar-collapsed .chat-main {
  max-width: 100%;
  position: relative;
}
.chat-layout.sidebar-collapsed .chat-main::before {
  content: '◀';
  position: absolute;
  left: 0;
  top: 50%;
  transform: translateY(-50%);
  width: 18px;
  height: 36px;
  cursor: pointer;
  z-index: 5;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 10px;
  color: var(--ink-lighter);
  background: var(--surface);
  border-radius: 0 6px 6px 0;
  border: 1px solid var(--border);
  border-left: none;
  opacity: 0;
  transition: opacity 0.2s;
}
.chat-layout.sidebar-collapsed .chat-main:hover::before {
  opacity: 1;
}
.auth-card .sq-section { margin-top:16px; padding-top:16px; border-top:1px solid var(--border-light); }
.auth-card .sq-section .sq-title { font-size:13px; color:var(--ink-light); margin-bottom:10px; font-weight:500; }
.auth-card .sq-row { margin-bottom:10px; }
.auth-card .sq-row select { width:100%; padding:8px 10px; border:1px solid var(--border); border-radius:8px; font-size:13px; font-family:inherit; outline:none; background:var(--bg); color:var(--ink); appearance:auto; }
.auth-card .sq-row select:focus { border-color:var(--accent); background:#fff; }
.auth-card .sq-row input { margin-top:6px; }
.auth-card .sq-step { display:none; }
.auth-card .sq-step.active { display:block; }
.auth-card .sq-btns { display:flex; gap:8px; margin-top:4px; }
.auth-card .sq-btns button { flex:1; padding:8px; border-radius:8px; font-size:13px; font-family:inherit; cursor:pointer; transition:all 0.15s; }
.auth-card .sq-btns .sq-prev { background:var(--bg); color:var(--ink-light); border:1px solid var(--border); }
.auth-card .sq-btns .sq-prev:hover { border-color:var(--ink-lighter); }
.auth-card .sq-btns .sq-next { background:var(--accent); color:#fff; border:none; }
.auth-card .sq-btns .sq-next:hover { background:#a37d4a; }
.auth-card .sq-register-btn { width:100%; padding:10px; background:var(--ink); color:#fff; border:none; border-radius:8px; font-size:14px; cursor:pointer; margin-top:4px; transition:background 0.15s; font-family:inherit; }
.auth-card .sq-register-btn:hover { background:#555; }
.auth-forgot { text-align:right; font-size:12px; margin-top:-8px; margin-bottom:8px; }
.auth-forgot a { color:var(--ink-lighter); cursor:pointer; text-decoration:none; }
.auth-forgot a:hover { color:var(--accent); }
.auth-card .fp-section { display:none; }
.auth-card .fp-section.active { display:block; }
.auth-card .fp-status { font-size:13px; color:var(--ink-light); margin:8px 0; text-align:center; }
@keyframes toolPulse { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:0.6;transform:scale(0.85)} }

/* ── Landing page (未登录) ── */
.landing-page {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  padding: 60px 40px;
  position: relative;
  overflow: hidden;
}
.landing-page::before {
  content: '策';
  font-family: var(--font-heading);
  font-size: 320px;
  font-weight: 700;
  color: var(--border-light);
  position: absolute;
  right: -40px;
  bottom: -40px;
  line-height: 1;
  opacity: 0.3;
  pointer-events: none;
  user-select: none;
}
.landing-page::after {
  content: '';
  position: absolute;
  left: 0; top: 0;
  width: 100%; height: 100%;
  background: radial-gradient(ellipse at 20% 40%, rgba(184,146,90,0.04) 0%, transparent 50%),
              radial-gradient(ellipse at 80% 60%, rgba(184,146,90,0.02) 0%, transparent 50%);
  pointer-events: none;
}
.landing-page .lp-inner {
  width: 100%;
  max-width: 880px;
  position: relative;
  z-index: 1;
}
.landing-page .lp-hero {
  margin-bottom: 48px;
}
.landing-page .lp-title-row {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-bottom: 12px;
}
.landing-page .lp-logo {
  flex-shrink: 0;
  width: 52px;
  height: 45px;
  color: var(--accent);
  animation: welcomeFade 0.6s ease both;
  animation-delay: 0.05s;
}
.landing-page .lp-logo svg {
  width: 100%;
  height: 100%;
}
.landing-page .lp-title {
  font-family: var(--font-heading);
  font-size: 42px;
  font-weight: 700;
  color: var(--ink);
  letter-spacing: 0.15em;
  line-height: 1.1;
  animation: welcomeFade 0.6s ease both;
  animation-delay: 0.1s;
}
.landing-page .lp-sub-line {
  font-size: 14px;
  letter-spacing: 0.04em;
  line-height: 1.6;
  animation: welcomeFade 0.6s ease both;
  animation-delay: 0.15s;
}
.landing-page .lp-sub-line .lp-accent {
  color: var(--accent);
}
.landing-page .lp-sub-line .lp-light {
  color: var(--ink-light);
}
.landing-page .lp-features {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
  margin-bottom: 36px;
  text-align: left;
  animation: welcomeFade 0.6s ease both;
  animation-delay: 0.25s;
}
.landing-page .lp-feature-card {
  background: var(--surface);
  border: 1px solid var(--border-light);
  border-radius: 12px;
  padding: 20px 18px;
  transition: all 0.3s ease;
  cursor: default;
}
.landing-page .lp-feature-card:hover {
  border-color: var(--accent-light);
  box-shadow: 0 6px 24px rgba(184,146,90,0.1);
  transform: translateY(-3px);
}
.landing-page .lp-feature-card .lp-fc-icon {
  width: 34px; height: 34px;
  border-radius: 9px;
  display: flex;
  align-items: center;
  justify-content: center;
  margin-bottom: 10px;
}
.landing-page .lp-feature-card .lp-fc-icon svg { width: 18px; height: 18px; }
.landing-page .lp-feature-card .lp-fc-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--ink);
  letter-spacing: 0.04em;
  margin-bottom: 6px;
}
.landing-page .lp-feature-card .lp-fc-desc {
  font-size: 12.5px;
  color: var(--ink-lighter);
  line-height: 1.6;
}
.landing-page .lp-actions {
  display: flex;
  gap: 14px;
  animation: welcomeFade 0.6s ease both;
  animation-delay: 0.25s;
}
.landing-page .lp-btn {
  padding: 12px 36px;
  border-radius: 10px;
  font-size: 15px;
  font-family: inherit;
  cursor: pointer;
  transition: all 0.25s ease;
  letter-spacing: 0.06em;
}
.landing-page .lp-btn-primary {
  background: var(--ink);
  color: #fff;
  border: none;
}
.landing-page .lp-btn-primary:hover {
  background: #555;
  transform: translateY(-2px);
  box-shadow: 0 4px 16px rgba(58,53,48,0.15);
}
.landing-page .lp-btn-secondary {
  background: transparent;
  color: var(--ink-light);
  border: 1px solid var(--border);
}
.landing-page .lp-btn-secondary:hover {
  border-color: var(--accent);
  color: var(--accent);
  background: var(--surface);
  transform: translateY(-2px);
}

@media (max-width: 700px) {
  .landing-page { padding: 40px 20px; }
  .landing-page .lp-logo { width: 40px; height: 34px; }
  .landing-page .lp-title { font-size: 28px; }
  .landing-page .lp-sub-line { font-size: 12px; }
  .landing-page .lp-features { gap: 10px; }
  .landing-page .lp-feature-card { padding: 14px 14px; }
  .landing-page::before { font-size: 180px; right: -20px; bottom: -20px; }
}

/* Auth overlay */
.auth-overlay {
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(58,53,48,0.3);
  backdrop-filter: blur(4px);
  z-index: 100;
  align-items: center;
  justify-content: center;
}
.auth-overlay.open { display: flex; }
.auth-card .auth-close {
  position: absolute;
  top: 12px; right: 12px;
  z-index: 10;
  background: rgba(0,0,0,0.06);
  border: none;
  font-size: 20px;
  color: var(--ink-light);
  cursor: pointer;
  width: 32px; height: 32px;
  display: flex; align-items: center; justify-content: center;
  border-radius: 50%;
  line-height: 1;
  transition: background 0.15s;
}
.auth-card .auth-close:hover {
  background: rgba(0,0,0,0.12);
  color: var(--ink);
}
.auth-overlay.open .auth-card {
  animation: authSlideIn 0.3s ease;
}
@keyframes authSlideIn {
  from { opacity: 0; transform: translateY(20px) scale(0.97); }
  to { opacity: 1; transform: translateY(0) scale(1); }
}

/* ── 新用户引导 ── */
.onboarding-overlay {
  position: fixed; inset: 0; z-index: 2000;
  background: rgba(0,0,0,0.5); backdrop-filter: blur(4px);
  display: none; align-items: center; justify-content: center;
  animation: fadeIn 0.3s ease;
}
.onboarding-overlay.open { display: flex; }
.onboarding-card {
  background: var(--bg,#fff); border-radius: 20px;
  padding: 40px 36px; width: 400px; max-width: 90vw;
  box-shadow: 0 20px 60px rgba(0,0,0,0.15);
  animation: authSlideIn 0.35s ease;
}
.ob-step { display: none; text-align: center; }
.ob-step.active { display: block; }
.ob-icon { font-size: 48px; margin-bottom: 12px; }
.ob-step h2 { font-size: 22px; font-weight: 700; color: var(--ink,#222); margin: 0 0 4px; }
.ob-sub { font-size: 14px; color: var(--ink-lighter,#999); margin: 0 0 24px; }
.ob-step input {
  width: 100%; padding: 12px 16px; font-size: 16px;
  border: 1.5px solid var(--border,#ddd); border-radius: 10px;
  outline: none; transition: border-color 0.2s; box-sizing: border-box;
}
.ob-step input:focus { border-color: var(--accent,#8b7355); }
.ob-btn {
  display: block; width: 100%; margin-top: 16px;
  padding: 12px; font-size: 15px; font-weight: 600;
  background: var(--ink,#222); color: #fff; border: none;
  border-radius: 10px; cursor: pointer; transition: opacity 0.2s;
}
.ob-btn:hover { opacity: 0.85; }
.ob-skip {
  display: inline-block; margin-top: 12px;
  font-size: 13px; color: var(--ink-lighter,#999);
  background: none; border: none; cursor: pointer;
}
.ob-skip:hover { color: var(--ink,#222); }
.ob-dims { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 8px; }
.ob-dims button {
  padding: 12px 8px; font-size: 14px; border: 1.5px solid var(--border,#ddd);
  border-radius: 10px; background: var(--bg,#fff); cursor: pointer;
  transition: all 0.2s; font-weight: 500;
}
.ob-dims button:hover { border-color: var(--accent,#8b7355); background: var(--accent-bg,#f5f0e8); }
.ob-dims button.selected { border-color: var(--accent,#8b7355); background: var(--accent,#8b7355); color: #fff; }
.ob-sq-row { margin-bottom: 12px; }
.ob-sq-row select, .ob-sa-input { width: 100%; padding: 10px 12px; font-size: 14px; border: 1.5px solid var(--border,#ddd); border-radius: 8px; outline: none; transition: border-color 0.2s; box-sizing: border-box; margin-bottom: 6px; }
.ob-sq-row select:focus, .ob-sa-input:focus { border-color: var(--accent,#8b7355); }
.ob-sa-input { margin-bottom: 0; }
@keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
</style>
</head>
<body>

<!-- ── Landing Page (未登录) ── -->
<div class="landing-page" id="landingPage">
  <div class="lp-inner">
    <div class="lp-hero">
      <div class="lp-title-row">
        <div class="lp-logo">
          <svg viewBox="0 -18 100 85" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
            <path d="M22 52 L22 28 Q22 -4 50 -10 Q78 -4 78 28 L78 52"/>
            <path d="M18 52 Q50 58 82 52" stroke-width="4"/>
            <path d="M18 48 Q50 54 82 48" stroke-width="1.8"/>
            <path d="M30 -6 L28 50" stroke-width="1.6" opacity="0.5"/>
            <path d="M38 -9 L36 51" stroke-width="1.6" opacity="0.5"/>
            <path d="M46 -10 L44 52" stroke-width="1.6" opacity="0.55"/>
            <path d="M54 -10 L56 52" stroke-width="1.6" opacity="0.55"/>
            <path d="M62 -9 L64 51" stroke-width="1.6" opacity="0.5"/>
            <path d="M70 -6 L72 50" stroke-width="1.6" opacity="0.5"/>
            <rect x="47" y="52" width="6" height="5" rx="1.5" fill="currentColor" stroke="none"/>
          </svg>
        </div>
        <div class="lp-title">诸葛策</div>
      </div>
      <div class="lp-sub-line">
        <span class="lp-accent">融合东方智慧与现代 AI</span>
        <span class="lp-light"> · 洞察自我、规划职场生涯</span>
      </div>
    </div>
    <div class="lp-features">
      <div class="lp-feature-card">
        <div class="lp-fc-icon" style="background:var(--accent-light);color:var(--accent);">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg>
        </div>
        <div class="lp-fc-title">生涯战略</div>
        <div class="lp-fc-desc">结合命理与规划，洞察人生方向，做出真正适合你的长期选择</div>
      </div>
      <div class="lp-feature-card">
        <div class="lp-fc-icon" style="background:#e8e0d8;color:#6b6258;">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="8" r="4"/><path d="M20 21a8 8 0 1 0-16 0"/></svg>
        </div>
        <div class="lp-fc-title">自我认知</div>
        <div class="lp-fc-desc">深度用户画像分析，发现优势盲区，建立清晰的自我定位</div>
      </div>
      <div class="lp-feature-card">
        <div class="lp-fc-icon" style="background:var(--accent-light);color:var(--accent);">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>
        </div>
        <div class="lp-fc-title">决策参谋</div>
        <div class="lp-fc-desc">关键选择时刻，多维分析利弊，让你每次决定都有底气</div>
      </div>
      <div class="lp-feature-card">
        <div class="lp-fc-icon" style="background:#e8e0d8;color:#6b6258;">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-9-9" stroke-linecap="round"/><path d="M21 3v6h-6"/></svg>
        </div>
        <div class="lp-fc-title">运势洞察</div>
        <div class="lp-fc-desc">把握时机节奏，顺势而为，在对的时间做对的事</div>
      </div>
    </div>
    <div class="lp-actions">
      <button class="lp-btn lp-btn-primary" id="landingLoginBtn">登录</button>
      <button class="lp-btn lp-btn-secondary" id="landingRegisterBtn">注册</button>
    </div>
  </div>
</div>

<!-- ── Auth Overlay ── -->
<div class="auth-overlay" id="authOverlay">
  <div class="auth-card login-mode" id="authCard">
    <button class="auth-close" id="authClose">✕</button>
    <div class="seal" id="authSeal">策</div>
    <h1 id="authTitle">诸葛策</h1>
    <div class="sub" id="authSub">谋定而后动</div>
    <div class="err" id="authErr"></div>
    <input type="text" id="authUser" placeholder="用户名" autocomplete="username">
    <input type="password" id="authPass" placeholder="密码" autocomplete="current-password">
    <button id="authBtn">登录</button>
    <div class="toggle" id="authToggle">没有账号？去注册</div>
  </div>
</div>

<!-- ── 新用户引导 ── -->
<div class="onboarding-overlay" id="onboardingOverlay">
  <div class="onboarding-card">
    <div class="ob-step active" data-step="0">
      <div class="ob-icon">🔒</div>
      <h2>设置密保问题（可选）</h2>
      <p class="ob-sub">忘记密码时可用它找回</p>
      <div id="obSQFields">
        <div class="ob-sq-row">
          <select id="obSQ1" class="ob-select"></select>
          <input type="text" id="obSA1" class="ob-sa-input" placeholder="答案">
        </div>
        <div class="ob-sq-row">
          <select id="obSQ2" class="ob-select"></select>
          <input type="text" id="obSA2" class="ob-sa-input" placeholder="答案">
        </div>
      </div>
      <button class="ob-btn" id="obSaveSQ">保存</button>
      <button class="ob-skip" id="obSkipSQ">跳过</button>
    </div>
    <div class="ob-step" data-step="1">
      <div class="ob-icon">👋</div>
      <h2>欢迎加入诸葛策</h2>
      <p class="ob-sub">先简单认识一下你</p>
      <input type="text" id="obName" placeholder="你叫什么名字？" maxlength="20">
      <button class="ob-btn" id="obNext1">下一步</button>
    </div>
    <div class="ob-step" data-step="2">
      <div class="ob-icon">📍</div>
      <h2>你在哪个城市？</h2>
      <p class="ob-sub">让我知道你的时区，方便以后问候</p>
      <input type="text" id="obCity" placeholder="城市" maxlength="20">
      <button class="ob-btn" id="obNext2">下一步</button>
      <button class="ob-skip" id="obSkip2">跳过</button>
    </div>
    <div class="ob-step" data-step="3">
      <div class="ob-icon">🎯</div>
      <h2>你最关注什么？</h2>
      <p class="ob-sub">选一个方向，以后随时可以调整</p>
      <div class="ob-dims" id="obDims">
        <button data-d="职场">💼 职场</button>
        <button data-d="创业">🚀 创业</button>
        <button data-d="财务">💰 财务</button>
        <button data-d="学习">📚 学习</button>
        <button data-d="健康">🏥 健康</button>
        <button data-d="家庭">👨‍👩‍👧‍👦 家庭</button>
        <button data-d="社交">🤝 社交</button>
        <button data-d="精神">🧘 精神</button>
      </div>
      <button class="ob-btn ob-start" id="obStart">开始使用</button>
      <button class="ob-skip" id="obSkip3">跳过</button>
    </div>
  </div>
</div>

<!-- ── App ── -->
<div class="app" id="app">
  <div class="top-bar">
    <div style="display:flex;align-items:center;gap:8px;cursor:pointer" id="menuBtn">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="var(--ink-lighter)" stroke-width="2"><path d="M3 12h18M3 6h18M3 18h18"/></svg>
    </div>
    <h1>诸葛策</h1>
    <div class="top-bar-right">
      <button class="insight-btn" id="insightBtn"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px"><path d="M12 20V10"/><path d="M18 20V4"/><path d="M6 20v-4"/></svg>我的洞察</button>
      <button class="bookmark-btn" id="bookmarkBtn"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:3px"><path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v16z"/></svg>我的收藏</button>
      <div class="user-info">
      <span id="userName"></span>
      <button class="logout-btn" id="logoutBtn">退出</button>
    </div>
  </div>
  </div>
  <div class="chat-layout" id="chatLayout">
    <div class="sidebar" id="sidebar">
      <button class="new-btn" id="newConvBtn">+ 新对话</button>
      <div class="conv-list" id="convList"></div>
      <div class="sidebar-footer">
        <button class="fb-btn" id="feedbackBtn">反馈建议</button>
      </div>
    </div>
    <div class="chat-main" id="chatMain">
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

<!-- ── 洞察模态框 ── -->
<div class="insight-overlay" id="insightOverlay">
  <div class="insight-card">
    <button class="insight-close" id="insightClose">✕</button>
    <div class="sp-body" id="spBody">
      <div class="sp-loading">加载中…</div>
    </div>
  </div>
</div>

<!-- ── 收藏模态框 ── -->
<div class="bm-overlay" id="bmOverlay">
  <div class="bm-card">
    <button class="bm-close" id="bmClose">✕</button>
    <h2>我的收藏</h2>
    <div class="bm-list" id="bmList">
      <div class="bm-empty">暂无收藏</div>
    </div>
    <div class="bm-pagination" id="bmPagination" style="display:none;"></div>
  </div>
</div>

<!-- ── 反馈模态框 ── -->
<div class="fb-overlay" id="fbOverlay">
  <div class="fb-card">
    <button class="fb-close" id="fbClose">✕</button>
    <h2>反馈建议</h2>
    <p class="fb-sub">帮助我们做得更好</p>
    <textarea id="fbContent" placeholder="说说你的想法…" rows="5"></textarea>
    <input type="text" id="fbContact" placeholder="联系方式（选填，微信/邮箱等）">
    <button id="fbSubmit">提交反馈</button>
    <div class="fb-success" id="fbSuccess" style="display:none;">
      <div class="fb-success-icon">✓</div>
      <p>感谢你的反馈！</p>
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
let bookmarkMap = new Map();
let pendingScrollMsgId = 0;
let bmPage = 1, bmPageSize = 10, bmTotal = 0;

const $ = id => document.getElementById(id);
const msgEl = $('messages');
const inputEl = $('input');
const sendBtn = $('sendBtn');
const stopBtn = $('stopBtn');
const inputArea = $('inputArea');
const BM_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v16z"/></svg>';

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
const SECURITY_QUESTIONS = [
  "你的小学名称是什么？",
  "你的初中名称是什么？",
  "你的高中名称是什么？",
  "你最喜欢的电影是什么？",
  "你最喜欢的书籍是什么？",
  "你最喜欢的动物是什么？",
  "你的出生城市是哪里？",
  "你母亲的姓氏是什么？",
  "你父亲的姓氏是什么？",
  "你的第一位班主任名字是什么？",
  "你最喜欢的食物是什么？",
  "你最想去旅游的国家是哪里？",
];

function openAuth(mode) {
  authMode = mode || 'login';
  const card = $('authCard');
  overlay.classList.add('open');
  // Reset to login mode
  card.classList.remove('register-mode');
  card.classList.add('login-mode');
  $('authBtn').textContent = '登录';
  $('authToggle').textContent = '没有账号？去注册';
  $('authSub').textContent = '谋定而后动';
  $('authTitle').textContent = '诸葛策';
  $('authUser').placeholder = '用户名';
  $('authPass').placeholder = '密码';
  $('authUser').value = '';
  $('authPass').value = '';
  $('authErr').style.display = 'none';
  setTimeout(() => $('authUser').focus(), 200);

  if (mode === 'register') {
    authMode = 'login';  // 重置让 toggle 正确翻转
    $('authToggle').click();
  }
}

const overlay = $('authOverlay');
$('landingLoginBtn').onclick = () => openAuth('login');
$('landingRegisterBtn').onclick = () => openAuth('register');
$('authClose').onclick = () => overlay.classList.remove('open');
overlay.onclick = (e) => { if (e.target === overlay) overlay.classList.remove('open'); };
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') overlay.classList.remove('open'); });

$('authToggle').onclick = () => {
  const isLogin = authMode === 'login';
  authMode = isLogin ? 'register' : 'login';
  const card = $('authCard');

  if (!isLogin) {
    card.classList.remove('register-mode');
    card.classList.add('login-mode');
    $('authBtn').textContent = '登录';
    $('authToggle').textContent = '没有账号？去注册';
    $('authSub').textContent = '谋定而后动';
    $('authTitle').textContent = '诸葛策';
    $('authUser').placeholder = '用户名';
    $('authPass').placeholder = '密码';
  } else {
    card.classList.remove('login-mode');
    card.classList.add('register-mode');
    $('authBtn').textContent = '创建账号';
    $('authToggle').textContent = '已有账号？去登录';
    $('authSub').textContent = '开启你的战略之旅';
    $('authTitle').textContent = '加入诸葛策';
    $('authUser').placeholder = '设置用户名';
    $('authPass').placeholder = '设置密码';
  }
  $('authErr').style.display = 'none';
};

// 实时检测用户名是否可用
let _usernameTimer = null;
$('authUser').oninput = () => {
  clearTimeout(_usernameTimer);
  const tip = $('authErr');
  // 登录模式不需要检测用户名是否可用
  if (authMode !== 'register') { tip.style.display = 'none'; return; }
  const u = $('authUser').value.trim();
  if (u.length < 2) { tip.style.display = 'none'; return; }
  _usernameTimer = setTimeout(async () => {
    const r = await (await fetch('/api/check-username?username=' + encodeURIComponent(u))).json();
    if (r.available) {
      tip.textContent = '✓ 用户名可用';
      tip.style.color = 'var(--accent,#8b7355)';
      tip.style.display = 'block';
    } else {
      tip.textContent = '✕ 用户名已被注册';
      tip.style.color = '#b33';
      tip.style.display = 'block';
    }
  }, 300);
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
  overlay.classList.remove('open');
  if (authMode === 'register') {
    // 新用户 → 引导流程
    document.getElementById('onboardingOverlay').classList.add('open');
  } else {
    initApp();
  }
};

// Enter to submit auth forms
$('authPass').onkeydown = e => { if (e.key === 'Enter' && authMode === 'login') $('authBtn').click(); };
$('authUser').onkeydown = e => { if (e.key === 'Enter') $('authPass').focus(); };

// ── 新用户引导流程 ──
const obOverlay = document.getElementById('onboardingOverlay');
let obData = {};

function showObStep(n) {
  document.querySelectorAll('.ob-step').forEach(el => el.classList.remove('active'));
  document.querySelector('.ob-step[data-step="'+n+'"]').classList.add('active');
}

// ── 密保问题（引导步骤0）──
// 准备两个 select，去重
function populateSQSelect(id, exclude) {
  const sel = document.getElementById(id);
  sel.innerHTML = '<option value="">-- 选择问题 --</option>' +
    SECURITY_QUESTIONS.map(q => '<option value="'+q+'"'+(q===exclude?' disabled':'')+'>'+q+'</option>').join('');
}
populateSQSelect('obSQ1', '');
$('obSQ1').onchange = () => populateSQSelect('obSQ2', $('obSQ1').value);
populateSQSelect('obSQ2', '');

$('obSaveSQ').onclick = async () => {
  const q1 = $('obSQ1').value, a1 = $('obSA1').value.trim();
  const q2 = $('obSQ2').value, a2 = $('obSA2').value.trim();
  if (!q1 || !q2 || !a1 || !a2) {
    $('obSaveSQ').textContent = '请选择问题和填写答案';
    setTimeout(() => $('obSaveSQ').textContent = '保存', 1500);
    return;
  }
  $('obSaveSQ').disabled = true;
  try {
    await fetch('/api/user-questions', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({questions: [
        {question: q1, answer: a1},
        {question: q2, answer: a2},
      ]}),
    });
  } catch(e) {}
  showObStep(1);
  setTimeout(() => $('obName').focus(), 200);
};
$('obSkipSQ').onclick = () => { showObStep(1); setTimeout(() => $('obName').focus(), 200); };

$('obNext1').onclick = () => {
  const name = $('obName').value.trim();
  if (!name) { $('obName').style.borderColor = '#b33'; return; }
  obData.name = name;
  showObStep(2);
  setTimeout(() => $('obCity').focus(), 200);
};
$('obName').onkeydown = e => { if (e.key === 'Enter') $('obNext1').click(); };
$('obName').oninput = () => $('obName').style.borderColor = '';

$('obNext2').onclick = () => {
  obData.city = $('obCity').value.trim();
  showObStep(3);
};
$('obSkip2').onclick = () => { obData.city = ''; showObStep(3); };
$('obCity').onkeydown = e => { if (e.key === 'Enter') $('obNext2').click(); };

// 维度选择
$('obDims').querySelectorAll('button').forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll('.ob-dims button').forEach(b => b.classList.remove('selected'));
    btn.classList.add('selected');
    obData.focus = btn.dataset.d;
  };
});

async function finishOnboarding() {
  $('obStart').disabled = true;
  $('obStart').textContent = '加载中…';
  try {
    const resp = await fetch('/api/onboard', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify(obData),
    });
    if (!resp.ok) throw new Error('onboard failed');
    const data = await resp.json();
    obOverlay.classList.remove('open');
    await initApp(data.conv_id, data.welcome);
  } catch(e) {
    obOverlay.classList.remove('open');
    initApp();
  }
}
$('obStart').onclick = finishOnboarding;
$('obSkip3').onclick = () => { obData.focus = ''; finishOnboarding(); };

// ── App Init ──
async function initApp(convId, welcomeMsg) {
  $('landingPage').style.display = 'none';
  overlay.classList.remove('open');
  $('app').classList.add('visible');
  const me = await (await fetch('/api/me')).json();
  $('userName').textContent = me.username;
  await loadConvs();
  // 预加载收藏
  try {
    const bms = await (await fetch('/api/bookmarks')).json();
    for (const bm of bms) bookmarkMap.set(bm.conversation_id + ':' + bm.message_id, bm.id);
  } catch(e) {}
  if (convId && welcomeMsg) {
    // 新用户引导完成，显示欢迎消息
    currentConvId = convId;
    msgEl.innerHTML = '';
    msgEl.querySelector('.welcome')?.remove();
    addMessage('assistant', marked.parse(welcomeMsg).replace(/<a\s+href=/g, '<a target="_blank" href='));
    requestAnimationFrame(() => msgEl.scrollTop = msgEl.scrollHeight);
    renderConvList();
  } else if (convs.length > 0) {
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
      if (m.role === 'user') addMessage('user', escapeHtml(m.content), m.content, m.id);
      else addMessage('agent', marked.parse(m.content), m.content, m.id);
    }
    // scroll to target message
    if (pendingScrollMsgId) {
      requestAnimationFrame(() => {
        const target = document.querySelector('.msg[data-msg-id="' + pendingScrollMsgId + '"]');
        if (target) target.scrollIntoView({ block: 'start', behavior: 'smooth' });
        pendingScrollMsgId = 0;
      });
    }
  } else {
    showWelcome(null);
  }
}

function showWelcome(name) {
  const userName = name ? escapeHtml(name) : null;
  msgEl.innerHTML = '<div class="welcome">' +
    '<div class="hero-title">' + (userName ? '你好，<em>' + userName + '</em>' : '知 <em>人</em> 者智，<br>自 <em>知</em> 者明') + '</div>' +
    '<div class="hero-desc">' + (userName
      ? '融合东方智慧与现代 AI，助你洞察自我、规划生涯、决胜未来。'
      : '融合东方智慧与现代 AI，助你洞察自我、规划生涯、决胜未来。') +
    '</div>' +
    '<div class="prompts">' +
      '<div class="prompts-label">试试这样问我</div>' +
      '<button class="prompt-chip" data-prompt="分析我的职业优势">分析我的职业优势</button>' +
      '<button class="prompt-chip" data-prompt="给我做一份年度战略规划">年度战略规划</button>' +
      '<button class="prompt-chip" data-prompt="看看我最近的运势">看看我最近的运势</button>' +
      '<button class="prompt-chip" data-prompt="帮我做个人物画像">帮我做个人物画像</button>' +
    '</div></div>';

  // Bind prompt chips
  msgEl.querySelectorAll('.prompt-chip').forEach(chip => {
    chip.onclick = () => {
      inputEl.value = chip.dataset.prompt;
      inputEl.style.height = 'auto';
      inputEl.style.height = Math.min(inputEl.scrollHeight, 280) + 'px';
      inputEl.focus();
      send();
    };
  });
}

// ── Chat ──
function addMessage(role, content, rawText, msgId) {
  const div = document.createElement('div');
  div.className = 'msg ' + role;
  if (msgId) div.dataset.msgId = msgId;
  const actionsHtml =
    '<button class="copy-btn" title="复制">' +
      '<svg viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2" stroke="currentColor" stroke-width="1.5" fill="none"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" stroke="currentColor" stroke-width="1.5" fill="none"/></svg>' +
    '</button>' +
    '<button class="regen-btn" title="重新生成">' +
      '<svg viewBox="0 0 24 24"><path d="M17.65 6.35A7.96 7.96 0 0 0 12 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08A5.99 5.99 0 0 1 12 18c-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z" fill="currentColor"/></svg>' +
    '</button>' +
    '<button class="edit-btn" title="编辑">' +
      '<svg viewBox="0 0 24 24"><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04a1 1 0 0 0 0-1.41l-2.34-2.34a1 1 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z" fill="currentColor"/></svg>' +
    '</button>' +
    '<button class="bm-btn" title="收藏">' + BM_SVG + '</button>';

  if (role === 'user') {
    div.dataset.rawText = rawText || content;
    div.innerHTML = '<div class="msg-bubble">' + content + '</div>';
    const actions = document.createElement('div');
    actions.className = 'msg-actions';
    actions.innerHTML = actionsHtml;
    div.appendChild(actions);
  } else {
    div.dataset.rawText = (rawText || content).replace(/<[^>]+>/g, '');
    div.innerHTML = '<div class="msg-agent-wrap">' + content + '</div>';
    const actions = document.createElement('div');
    actions.className = 'msg-actions';
    actions.innerHTML = '<button class="bm-btn" title="收藏">' + BM_SVG + '</button>';
    div.appendChild(actions);
  }
  msgEl.appendChild(div);
  requestAnimationFrame(() => msgEl.scrollTop = msgEl.scrollHeight);

  // Bind bookmark handler
  const bmBtn = div.querySelector('.bm-btn');
  if (bmBtn) bmBtn.onclick = () => toggleBookmark(div, role);

  // Bind user-specific handlers
  if (role === 'user') {
    const raw = div.dataset.rawText;
    div.querySelector('.copy-btn').onclick = () => {
      navigator.clipboard.writeText(raw).catch(() => {});
    };
    div.querySelector('.regen-btn').onclick = () => regenerate(div);
    div.querySelector('.edit-btn').onclick = () => startEdit(div);
    if (rawText != null) lastUserMsgEl = div;
  }

  // Update bookmark state
  if (msgId && currentConvId) updateBookmarkButtonState(div);

  return div;
}

/**
 * 更新 agent 消息框的显示内容，确保 .msg-agent-wrap（背景/圆角/内边距）
 * 和 .msg-actions（收藏按钮）始终存在。
 */
function updateAgentContent(msgDiv, htmlContent) {
  let wrap = msgDiv.querySelector('.msg-agent-wrap');
  if (!wrap) {
    msgDiv.innerHTML = '<div class="msg-agent-wrap"></div>'
      + '<div class="msg-actions"><button class="bm-btn" title="收藏">' + BM_SVG + '</button></div>';
    wrap = msgDiv.querySelector('.msg-agent-wrap');
    const bm = msgDiv.querySelector('.bm-btn');
    if (bm) bm.onclick = () => toggleBookmark(msgDiv, 'agent');
  }
  wrap.innerHTML = htmlContent;
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
      '<button class="bm-btn" title="收藏">' + BM_SVG + '</button>' +
    '</div>';
  msgDiv.querySelector('.copy-btn').onclick = () => navigator.clipboard.writeText(text).catch(() => {});
  msgDiv.querySelector('.regen-btn').onclick = () => regenerate(msgDiv);
  msgDiv.querySelector('.edit-btn').onclick = () => startEdit(msgDiv);
  msgDiv.querySelector('.bm-btn').onclick = () => toggleBookmark(msgDiv, 'user');
  if (currentConvId && msgDiv.dataset.msgId) updateBookmarkButtonState(msgDiv);
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

// ── 收藏 ──

async function toggleBookmark(msgDiv, role) {
  const msgId = parseInt(msgDiv.dataset.msgId);
  if (!msgId || !currentConvId) return;
  const key = currentConvId + ':' + msgId;
  const bmBtn = msgDiv.querySelector('.bm-btn');
  if (bookmarkMap.has(key)) {
    const bmId = bookmarkMap.get(key);
    await fetch('/api/bookmarks/' + bmId, {method: 'DELETE'});
    bookmarkMap.delete(key);
    if (bmBtn) bmBtn.classList.remove('bookmarked');
  } else {
    const preview = msgDiv.dataset.rawText || msgDiv.textContent || '';
    const resp = await fetch('/api/bookmarks', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        conversation_id: currentConvId,
        message_id: msgId,
        role: role,
        content_preview: preview,
      }),
    });
    const data = await resp.json();
    if (data.created !== false) {
      bookmarkMap.set(key, data.id);
      if (bmBtn) bmBtn.classList.add('bookmarked');
    }
  }
}

function updateBookmarkButtonState(msgDiv) {
  const msgId = parseInt(msgDiv.dataset.msgId);
  if (!msgId || !currentConvId) return;
  const key = currentConvId + ':' + msgId;
  const bmBtn = msgDiv.querySelector('.bm-btn');
  if (bmBtn && bookmarkMap.has(key)) {
    bmBtn.classList.add('bookmarked');
  }
}

async function renderBookmarkList() {
  $('bmOverlay').classList.add('open');
  const list = $('bmList');
  list.innerHTML = '<div class="bm-empty">加载中…</div>';

  // 隐藏分页栏
  const pEl = $('bmPagination');
  if (pEl) pEl.style.display = 'none';

  let data;
  try {
    const resp = await fetch('/api/bookmarks?page=' + bmPage + '&page_size=' + bmPageSize);
    data = await resp.json();
  } catch {
    list.innerHTML = '<div class="bm-empty">加载失败</div>';
    return;
  }

  const bookmarks = data.items;
  bmTotal = data.total;

  // 同步 bookmarkMap
  bookmarkMap.clear();
  for (const bm of bookmarks) {
    bookmarkMap.set(bm.conversation_id + ':' + bm.message_id, bm.id);
  }
  document.querySelectorAll('.msg[data-msg-id]').forEach(el => updateBookmarkButtonState(el));

  if (bookmarks.length === 0) {
    list.innerHTML = (bmPage === 1 ? '<div class="bm-empty">暂无收藏</div>' : '<div class="bm-empty">没有更多了</div>');
    renderPagination();
    return;
  }

  list.innerHTML = bookmarks.map(bm =>
    '<div class="bm-item' + (bm.conv_exists ? '' : ' conv-deleted') + '" data-id="' + bm.id + '" data-conv-id="' + bm.conversation_id + '" data-msg-id="' + bm.message_id + '">' +
      '<div class="bm-item-top">' +
        '<span class="bm-item-date">' + (bm.created_at || '').slice(0, 16).replace('T', ' ') + '</span>' +
        (bm.role === 'user' ? '<span class="bm-item-role user">我</span>' : '') +
      '</div>' +
      '<div class="bm-item-preview">' + escapeHtml(bm.content_preview) + '</div>' +
      '<div class="bm-item-full">' + marked.parse(bm.content_preview) + '</div>' +
      '<div class="bm-item-meta">' +
        (bm.conv_exists
          ? '<button class="bm-item-jump" title="跳转至对话">↗ 跳转对话</button>'
          : '<span class="bm-item-deleted-tag">对话已删除</span>') +
        '<button class="bm-item-del" data-bookmark-id="' + bm.id + '" title="取消收藏">✕</button>' +
      '</div>' +
    '</div>'
  ).join('');

  // 展开/收起 & 跳转
  list.querySelectorAll('.bm-item').forEach(el => {
    const convId = parseInt(el.dataset.convId);
    el.onclick = (e) => {
      if (e.target.classList.contains('bm-item-del')) return;
      if (e.target.classList.contains('bm-item-jump')) {
        pendingScrollMsgId = parseInt(el.dataset.msgId);
        $('bmOverlay').classList.remove('open');
        selectConv(convId);
        return;
      }
      el.classList.toggle('expanded');
    };
  });

  // 删除按钮（二次确认）
  list.querySelectorAll('.bm-item-del').forEach(btn => {
    btn.onclick = async (e) => {
      e.stopPropagation();
      if (!confirm('确定取消收藏？')) return;
      const bmId = parseInt(btn.dataset.bookmarkId);
      for (const [key, id] of bookmarkMap) {
        if (id === bmId) { bookmarkMap.delete(key); break; }
      }
      await fetch('/api/bookmarks/' + bmId, {method: 'DELETE'});
      document.querySelectorAll('.msg[data-msg-id]').forEach(el => updateBookmarkButtonState(el));
      renderBookmarkList();
    };
  });

  renderPagination();
}

function renderPagination() {
  const pEl = $('bmPagination');
  if (!pEl) return;
  const totalPages = Math.ceil(bmTotal / bmPageSize) || 1;
  if (totalPages <= 1 && bmPage === 1) { pEl.style.display = 'none'; return; }
  pEl.style.display = 'flex';

  let html = '<div class="bm-pg-info">共 ' + bmTotal + ' 条</div><div class="bm-pg-pages">';
  if (bmPage > 1) html += '<button class="bm-pg-btn" data-page="' + (bmPage-1) + '">‹</button>';
  html += '<span class="bm-pg-current">' + bmPage + '/' + totalPages + '</span>';
  if (bmPage < totalPages) html += '<button class="bm-pg-btn" data-page="' + (bmPage+1) + '">›</button>';
  html += '</div><div class="bm-pg-size"><select id="bmPageSizeSel">';
  [5, 10, 20, 50].forEach(s => html += '<option value="' + s + '"' + (s === bmPageSize ? ' selected' : '') + '>' + s + '条/页</option>');
  html += '</select></div>';

  pEl.innerHTML = html;

  pEl.querySelectorAll('.bm-pg-btn').forEach(btn => {
    btn.onclick = () => { bmPage = parseInt(btn.dataset.page); renderBookmarkList(); };
  });
  const sel = $('bmPageSizeSel');
  if (sel) sel.onchange = () => { bmPageSize = parseInt(sel.value); bmPage = 1; renderBookmarkList(); };
}

inputEl.oninput = () => {
  inputEl.style.height = 'auto';
  inputEl.style.height = Math.min(inputEl.scrollHeight, 280) + 'px';
};
inputEl.onkeydown = e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
};
sendBtn.onclick = () => send();

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
  let sseTimedOut = false;
  stopBtn.onclick = () => { abortController.abort(); };
  // SSE 超时：30 秒未收到完整响应则切到非流式
  const sseTimer = setTimeout(() => { sseTimedOut = true; abortController.abort(); }, 30000);

  try {
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({message: text, conversation_id: currentConvId}),
      signal: abortController.signal,
    });
    if (!resp.ok) {
      if (resp.status === 403) {
        const errData = await resp.json();
        throw new Error('QUOTA_EXCEEDED:' + (errData.detail || '免费体验已达上限'));
      }
      throw new Error('SSE_FAILED:' + resp.status);
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      // 收到数据则重置超时
      clearTimeout(sseTimer);
      for (const line of decoder.decode(value, {stream:true}).split('\n')) {
        if (!line.startsWith('data: ')) continue;
        const data = line.slice(6);
        if (data === '[DONE]') break;
        try {
          const p = JSON.parse(data);
          if (p.type === 'text') {
            hideToolStatus();
            content += p.content;
            updateAgentContent(msgDiv, marked.parse(content).replace(/<a\s+href=/g, '<a target="_blank" href='));
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
          } else if (p.type === 'user_msg_id') {
            if (lastUserMsgEl) {
              lastUserMsgEl.dataset.msgId = p.message_id;
              updateBookmarkButtonState(lastUserMsgEl);
            }
          } else if (p.type === 'assistant_msg_id') {
            msgDiv.dataset.msgId = p.message_id;
            updateBookmarkButtonState(msgDiv);
          }
        } catch(e) {}
      }
      requestAnimationFrame(() => msgEl.scrollTop = msgEl.scrollHeight);
    }
    clearTimeout(sseTimer);
  } catch(e) {
    clearTimeout(sseTimer);
    if (e.name === 'AbortError' && !sseTimedOut) {
      // 用户主动停止
      wasAborted = true;
      hideToolStatus();
      if (content) {
        updateAgentContent(msgDiv, marked.parse(content + '\n\n> ⏸️ **已停止**').replace(/<a\s+href=/g, '<a target="_blank" href='));
      } else {
        msgDiv.innerHTML = '<p style="color:var(--ink-lighter);font-style:italic;font-size:13px;">⏸️ 已停止</p>';
      }
    } else if (e.message && e.message.startsWith('QUOTA_EXCEEDED:')) {
      const detail = e.message.slice('QUOTA_EXCEEDED:'.length);
      msgDiv.innerHTML = '<div style="text-align:center;padding:24px 16px;color:var(--ink-lighter);">'
        + '<svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" style="opacity:0.4;margin-bottom:8px;"><circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/></svg>'
        + '<p style="font-size:15px;margin:4px 0;">' + escapeHtml(detail) + '</p>'
        + '<p style="font-size:12px;margin:4px 0;">如需继续使用，请联系作者</p>'
        + '</div>';
      inputEl.disabled = true;
      sendBtn.disabled = true;
    } else {
      // SSE 失败 → 降级到非流式接口
      hideToolStatus();
      updateAgentContent(msgDiv, '<div class="thinking-dots"><span></span><span></span><span></span></div>');
      try {
        const fbResp = await fetch('/api/send', {
          method: 'POST',
          headers: {'Content-Type':'application/json'},
          body: JSON.stringify({message: text, conversation_id: currentConvId}),
          signal: abortController.signal,
        });
        if (!fbResp.ok) {
          if (fbResp.status === 403) throw new Error('QUOTA_EXCEEDED:' + ((await fbResp.json()).detail || ''));
          throw new Error('fallback failed');
        }
        const fbData = await fbResp.json();
        content = fbData.response || '';
        updateAgentContent(msgDiv, marked.parse(content).replace(/<a\s+href=/g, '<a target="_blank" href='));
        if (fbData.user_msg_id && lastUserMsgEl) {
          lastUserMsgEl.dataset.msgId = fbData.user_msg_id;
          updateBookmarkButtonState(lastUserMsgEl);
        }
        if (fbData.assistant_msg_id) {
          msgDiv.dataset.msgId = fbData.assistant_msg_id;
          updateBookmarkButtonState(msgDiv);
        }
        requestAnimationFrame(() => msgEl.scrollTop = msgEl.scrollHeight);
      } catch(fbErr) {
        if (fbErr.message && fbErr.message.startsWith('QUOTA_EXCEEDED:')) {
          const detail = fbErr.message.slice('QUOTA_EXCEEDED:'.length);
          msgDiv.innerHTML = '<div style="text-align:center;padding:24px 16px;color:var(--ink-lighter);">'
            + '<svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" style="opacity:0.4;margin-bottom:8px;"><circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/></svg>'
            + '<p style="font-size:15px;margin:4px 0;">' + escapeHtml(detail) + '</p>'
            + '<p style="font-size:12px;margin:4px 0;">如需继续使用，请联系作者</p>'
            + '</div>';
          inputEl.disabled = true;
          sendBtn.disabled = true;
        } else {
          msgDiv.innerHTML = '<p style="color:#b33;">连接失败，请确认服务器正在运行</p>';
        }
      }
    }
  }

  restoreInput();
  if (!wasAborted) loadConvs();
}

// ── 初始检查 ──
// Landing page is visible by default; show app if already logged in
async function checkAuth() {
  const me = await (await fetch('/api/me')).json();
  if (me.authenticated) {
    $('landingPage').style.display = 'none';
    initApp();
  }
}

// ── 登出 ──
$('logoutBtn').onclick = async () => {
  await fetch('/api/logout', {method:'POST'});
  $('app').classList.remove('visible');
  $('landingPage').style.display = '';
  currentConvId = 0;
  convs = [];
  msgEl.innerHTML = '';
};

// ── Sidebar toggle ──
function toggleSidebar() {
  if (window.innerWidth <= 700) {
    $('sidebar').classList.toggle('open');
  } else {
    $('sidebar').classList.toggle('collapsed');
    $('chatLayout').classList.toggle('sidebar-collapsed');
  }
}
$('menuBtn').onclick = toggleSidebar;
// 侧边栏收起时，点击页面左侧也可展开
$('chatLayout').onclick = (e) => {
  if ($('sidebar').classList.contains('collapsed') && e.clientX < 30) {
    toggleSidebar();
  }
};

checkAuth();

function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

// ── 职业洞察页 ──
let cachedDashData = null;

async function renderStrategyPage() {
  $('insightOverlay').classList.add('open');
  $('spBody').innerHTML = '<div class="sp-loading"><div class="sp-loading-text">诸葛策分析中…</div><div class="sp-loading-bar-wrap"><div class="sp-loading-bar" id="insightProgressBar"></div></div></div>';

  // 并行：取数据 + 等进度条动画
  const [data] = await Promise.all([
    (async () => {
      try {
        const res = await fetch('/api/dashboard');
        if (!res.ok) return null;
        const d = await res.json();
        return d.position.name ? d : null;
      } catch { return null; }
    })(),
    new Promise(resolve => {
      const bar = document.getElementById('insightProgressBar');
      if (bar) {
        bar.addEventListener('animationend', resolve, {once: true});
      } else {
        resolve();
      }
    }),
  ]);

  if (!data) {
    $('spBody').innerHTML = '<div class="sp-loading"><div class="sp-loading-text">暂无数据，多聊聊建立你的职业档案吧</div></div>';
    return;
  }
  cachedDashData = data;
  renderStrategyData(data);
}

function closeInsight() {
  $('insightOverlay').classList.remove('open');
}

// 洞察弹窗关闭
$('insightClose').onclick = closeInsight;
$('insightOverlay').onclick = (e) => { if (e.target === $('insightOverlay')) closeInsight(); };
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeInsight(); });

function renderStrategyData(data) {
  const pos = data.position;
  const body = $('spBody');

  let html = '';

  // 职位卡
  const avatar = (pos.name || '?')[0];
  html += `<div class="sp-card"><div class="sp-pos-row"><div class="sp-avatar">${avatar}</div><div class="sp-pos-info"><div class="sp-pos-name">${esc(pos.name)}</div><div class="sp-pos-title">${esc([pos.title, pos.company, pos.level].filter(Boolean).join(' · '))}${pos.city ? ' · '+esc(pos.city) : ''}</div>${pos.identity ? '<div class="sp-pos-identity">'+esc(pos.identity)+'</div>' : ''}</div></div></div>`;

  // 阶段
  const st = data.stage;
  html += `<div class="sp-card"><h3>职业阶段</h3><div class="sp-stage-row"><span class="sp-stage-label">${esc(st.label)}</span><div class="sp-stage-bar"><div class="sp-stage-fill" style="width:${st.progress}%"></div></div><span class="sp-stage-pct">${st.progress}%</span></div></div>`;

  // 技能
  if (data.skills && data.skills.length) {
    html += `<div class="sp-card"><h3>核心能力</h3><div class="sp-skills">${data.skills.map(s => `<span class="sp-skill-tag">${esc(s)}</span>`).join('')}</div></div>`;
  }

  // 目标
  if (data.goals && data.goals.length) {
    html += `<div class="sp-card"><h3>当前目标</h3><ul class="sp-goal-list">${data.goals.map(g => `<li class="sp-goal-item"><span class="sp-goal-dot ${g.status}"></span>${esc(g.title)}</li>`).join('')}</ul></div>`;
  }

  // 关联维度
  if (data.dimensions) {
    html += `<div class="sp-card"><h3>关联维度</h3><div class="sp-dim-grid">${Object.values(data.dimensions).map(d => `<div class="sp-dim-item"><span class="sp-dim-dot ${d.status}"></span>${esc(d.label)}</div>`).join('')}</div></div>`;
  }

  // 里程碑
  if (data.milestones && data.milestones.length) {
    html += `<div class="sp-card"><h3>决策与里程碑</h3><div class="sp-ms-list">${data.milestones.map(m => `<div class="sp-ms-item"><span class="sp-ms-icon">${m.type === 'achievement' ? '✦' : '◆'}</span><span class="sp-ms-text">${esc(m.title)}</span>${m.date ? '<span class="sp-ms-date">'+esc(m.date)+'</span>' : ''}</div>`).join('')}</div></div>`;
  }

  body.innerHTML = html;
}

// ── 顶部导航 ──
$('insightBtn').onclick = renderStrategyPage;
// ── 收藏 ──
$('bookmarkBtn').onclick = renderBookmarkList;
$('bmClose').onclick = () => $('bmOverlay').classList.remove('open');
$('bmOverlay').onclick = (e) => {
  if (e.target === $('bmOverlay')) $('bmOverlay').classList.remove('open');
};
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    $('bmOverlay').classList.remove('open');
    $('insightOverlay').classList.remove('open');
  }
});
// ── 反馈 ──
$('feedbackBtn').onclick = () => $('fbOverlay').classList.add('open');
$('fbClose').onclick = () => $('fbOverlay').classList.remove('open');
$('fbOverlay').onclick = (e) => {
  if (e.target === $('fbOverlay')) $('fbOverlay').classList.remove('open');
};
$('fbSubmit').onclick = async () => {
  const content = $('fbContent').value.trim();
  if (!content) return;
  $('fbSubmit').disabled = true;
  $('fbSubmit').textContent = '提交中…';
  try {
    await fetch('/api/feedback', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({content, contact: $('fbContact').value.trim()}),
    });
    $('fbContent').style.display = 'none';
    $('fbContact').style.display = 'none';
    $('fbSubmit').style.display = 'none';
    $('fbSuccess').style.display = 'block';
  } catch {
    $('fbSubmit').disabled = false;
    $('fbSubmit').textContent = '提交失败，重试';
  }
};
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
