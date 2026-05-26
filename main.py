#!/usr/bin/env python3
"""诸葛策 — 人生发展 Agent CLI"""

import io
import os
import sys
import json
import re
import random
import shutil
import select
import termios
import tty
import threading
import time
import atexit
import signal
from datetime import datetime
from pathlib import Path
from typing import Optional

# 兜底：stdout 遇到无效字符用 ? 替代而非崩溃
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ── 终端安全：确保程序退出时恢复终端 ──
_saved_termios = [None]  # list 包裹以便闭包修改
_saved_termios_fd = [None]


def _restore_termios_atexit():
    """退出时恢复终端（无论如何退出）。"""
    fd = _saved_termios_fd[0]
    old = _saved_termios[0]
    if fd is not None and old is not None:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            pass


atexit.register(_restore_termios_atexit)
signal.signal(signal.SIGTERM, lambda *a: _restore_termios_atexit() or sys.exit(1))
signal.signal(signal.SIGHUP, lambda *a: _restore_termios_atexit() or sys.exit(1))


# ── 移除 surrogate 字符 ──
def _sanitize(text: str) -> str:
    return ''.join(ch for ch in text if not (0xD800 <= ord(ch) <= 0xDFFF))


# ── 思考动画 ──

class Spinner:
    """动态思考指示器，在后台线程运行。显示已耗时和步骤进度。"""
    CHARS = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'

    def __init__(self, text="思考中", step: int = 0, total_steps: int = None):
        self.text = text
        self._running = False
        self._thread = None
        self._start = time.time()
        self._step = step
        self._total = total_steps

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(0.3)

    def _run(self):
        i = 0
        while self._running:
            elapsed = int(time.time() - self._start)
            ch = self.CHARS[i % len(self.CHARS)]
            parts = [f"{ch} {self.text}"]
            if self._step > 0:
                parts.append(f"#{self._step}")
                if self._total:
                    parts[-1] += f"/{self._total}"
            if elapsed >= 3:
                parts.append(f"{elapsed}s")
            sys.stdout.write(f"\r{C.DIM}{' '.join(parts)}…{C.RESET}")
            sys.stdout.flush()
            i += 1
            time.sleep(0.08)


class InputBuffer:
    """在 Agent 思考时在后台线程读取用户键盘输入。"""

    def __init__(self, fd: int):
        self.fd = fd
        self._buffer = []
        self._lock = threading.Lock()
        self._running = False
        self._thread = None

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(0.3)

    def _run(self):
        while self._running:
            ready, _, _ = select.select([self.fd], [], [], 0.1)
            if ready:
                chunk = os.read(self.fd, 65536)
                if chunk:
                    with self._lock:
                        self._buffer.append(chunk)

    def get_and_clear(self) -> str:
        with self._lock:
            raw = b"".join(self._buffer)
            self._buffer.clear()
        if not raw:
            return ""
        text = raw.decode("utf-8", errors="replace")
        while "\x7f" in text:
            text = re.sub(r".\x7f", "", text, count=1)
        text = text.replace("\x7f", "")
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        return " ".join(lines).strip()


def _clear_line():
    """清除当前行。"""
    sys.stdout.write("\r\033[K")
    sys.stdout.flush()


def _term_thinking_mode(fd: int):
    """将终端设为思考模式：非规范、不回显。"""
    old = None
    try:
        old = termios.tcgetattr(fd)
        new = termios.tcgetattr(fd)
        new[tty.LFLAG] &= ~(termios.ICANON | termios.ECHO)
        new[tty.IFLAG] |= termios.ICRNL
        new[tty.CC][termios.VMIN] = 1
        new[tty.CC][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSADRAIN, new)
    except termios.error:
        pass
    return old


def _restore_terminal(fd: int, old):
    if old is not None:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except termios.error:
            pass


def _is_cjk(ch: str) -> bool:
    """CJK 统一表意文字占 2 列"""
    cp = ord(ch)
    return (0x4E00 <= cp <= 0x9FFF) or (0x3000 <= cp <= 0x303F) or (0xFF00 <= cp <= 0xFFEF)


def _read_input(prompt: str) -> str:
    """非规范模式输入：支持粘贴、光标移动、中间插入、回退。"""
    fd = sys.stdin.fileno()
    old = None
    try:
        old = termios.tcgetattr(fd)
        new = termios.tcgetattr(fd)
        new[tty.LFLAG] &= ~(termios.ICANON | termios.ECHO)
        new[tty.IFLAG] |= termios.ICRNL
        new[tty.CC][termios.VMIN] = 1
        new[tty.CC][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSADRAIN, new)
        _saved_termios[0] = old
        _saved_termios_fd[0] = fd
    except termios.error:
        pass

    buf = []          # 字符缓冲区
    cursor = 0        # 光标在 buf 中的位置（字符索引）
    partial = b''
    last_read_size = 0

    # ── 辅助函数 ──

    def _char_width(ch: str) -> int:
        return 2 if _is_cjk(ch) else 1

    def _buf_visible(end=None) -> int:
        """buf[:end] 的终端显示宽度"""
        return sum(_char_width(c) for c in buf[:end])

    def _redraw():
        """重绘整行并定位光标。"""
        prompt_w = _render_width(prompt)
        col = prompt_w + _buf_visible(cursor)
        sys.stdout.write("\r\033[K")
        sys.stdout.write(prompt)
        sys.stdout.write("".join(buf))
        sys.stdout.write(f"\033[{col + 1}G")
        sys.stdout.flush()

    def submit() -> str:
        result = "".join(buf).strip()
        if result and len(result) > 100:
            print(f"{C.DIM}[已接收 {len(result)} 字符]{C.RESET}")
        return result

    def save_termios():
        nonlocal old
        if old is not None:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
            except termios.error:
                pass

    # 初始绘制
    _redraw()

    # ── 逐字节解析 ──

    try:
        while True:
            raw = os.read(fd, 65536)
            if not raw:
                break
            last_read_size = len(raw)
            partial += raw

            proc = 0
            changed = False  # 批量处理后再重绘

            while proc < len(partial):
                b = partial[proc]

                # ── 转义序列（箭头键等）──
                if b == 27:
                    if (proc + 2 < len(partial)
                            and partial[proc + 1] == ord("[")):
                        cmd = partial[proc + 2]
                        if cmd == ord("D"):  # ←
                            if cursor > 0:
                                cursor -= 1
                                _redraw()
                        elif cmd == ord("C"):  # →
                            if cursor < len(buf):
                                cursor += 1
                                _redraw()
                        elif cmd == ord("H") or cmd == ord("1"):  # Home
                            cursor = 0
                            _redraw()
                        elif cmd == ord("F") or cmd == ord("4"):  # End
                            cursor = len(buf)
                            _redraw()
                        proc += 3
                        continue
                    proc += 1
                    continue

                # ── Enter ──
                if b == 10:
                    if proc < len(partial) - 1:
                        # 粘贴换行 → 空格
                        buf.insert(cursor, " ")
                        cursor += 1
                        changed = True
                        proc += 1
                        continue
                    timeout = 0.5 if last_read_size > 200 else 0.15
                    ready, _, _ = select.select([fd], [], [], timeout)
                    if not ready:
                        save_termios()
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                        return submit()
                    buf.insert(cursor, " ")
                    cursor += 1
                    changed = True
                    proc += 1
                    continue

                if b == 13:  # \r
                    proc += 1
                    continue

                # ── Backspace ──
                if b in (127, 8):
                    if cursor > 0:
                        cursor -= 1
                        del buf[cursor]
                        _redraw()
                    proc += 1
                    continue

                if b == 3:  # Ctrl+C
                    save_termios()
                    raise KeyboardInterrupt

                if b == 4:  # Ctrl+D
                    proc += 1
                    continue

                if b < 32:  # 其他控制字符
                    proc += 1
                    continue

                # ── 可打印字符（ASCII / 多字节 UTF-8）──
                if b & 0x80 == 0:  # ASCII
                    ch = chr(b)
                    buf.insert(cursor, ch)
                    cursor += 1
                    changed = True
                    proc += 1
                else:  # 多字节 UTF-8
                    if b & 0xE0 == 0xC0:
                        need = 2
                    elif b & 0xF0 == 0xE0:
                        need = 3
                    elif b & 0xF8 == 0xF0:
                        need = 4
                    else:
                        proc += 1
                        continue
                    if proc + need > len(partial):
                        break
                    try:
                        ch = partial[proc:proc + need].decode("utf-8")
                        buf.insert(cursor, ch)
                        cursor += 1
                        changed = True
                        proc += need
                    except UnicodeDecodeError:
                        proc += 1
                        continue

            partial = partial[proc:]
            if changed:
                _redraw()

    except KeyboardInterrupt:
        return ""
    except Exception:
        pass
    finally:
        if old is not None:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
            except termios.error:
                pass
        try:
            import fcntl
            fl = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
            os.read(fd, 65536)
        except Exception:
            pass
        finally:
            try:
                fcntl.fcntl(fd, fcntl.F_SETFL, fl)
            except Exception:
                pass

    return submit()


from agent import MingYuanAgent
from memory import load_profile, set_user, get_user, get_user_dir


# ── 成功谚语 ──
QUOTES = [
    "千里之行，始于足下。",
    "不积跬步，无以至千里。",
    "志当存高远。",
    "业精于勤，荒于嬉。",
    "谋事在人，成事在天。",
    "天行健，君子以自强不息。",
    "博观而约取，厚积而薄发。",
    "工欲善其事，必先利其器。",
    "凡事预则立，不预则废。",
    "知人者智，自知者明。",
    "三思而后行。",
    "温故而知新。",
    "非淡泊无以明志，非宁静无以致远。",
    "学而不思则罔，思而不学则殆。",
    "天下难事，必作于易；天下大事，必作于细。",
    "胜人者有力，自胜者强。",
]


def random_quote() -> str:
    return random.choice(QUOTES)


def _days_since_last() -> Optional[int]:
    from memory import load_recent_journal
    entries = load_recent_journal(1)
    if entries:
        saved = entries[0].get("_saved_at", "")
        if saved:
            try:
                last = datetime.fromisoformat(saved)
                return (datetime.now() - last).days
            except ValueError:
                pass
    return None


def _pending_decisions_count() -> int:
    from memory import load_decisions
    return sum(1 for d in load_decisions(50) if d.get("status") == "analyzing")


# ── ANSI 颜色 ──
class C:
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    MAGENTA = "\033[35m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"


def c(s: str, *codes: str) -> tuple:
    """返回 (带样式字符串, 可见长度)"""
    if codes:
        return ("".join(codes) + s + C.RESET, len(s))
    return (s, len(s))


def _display_width(s: str) -> int:
    """计算字符串在终端中的显示宽度（CJK 字符占 2 列）"""
    width = 0
    for ch in s:
        cp = ord(ch)
        if (0x4E00 <= cp <= 0x9FFF) or (0x3000 <= cp <= 0x303F) or \
           (0xFF00 <= cp <= 0xFFEF):
            width += 2
        # → (0x2192) 等箭头符号为 1 列
        else:
            width += 1
    return width


_ansi_pat = re.compile(r"\033\[[0-9;]*m")


def _render_width(s: str) -> int:
    """去除 ANSI 码后的终端显示宽度"""
    return _display_width(_ansi_pat.sub("", s))


def draw_box(rows: list, width: int = 68) -> None:
    """绘制方框，自动处理 ANSI 颜色和 CJK 字符宽度。"""
    w = min(shutil.get_terminal_size().columns, width)
    print(f"╔{'═' * (w - 2)}╗")
    for parts in rows:
        if not parts:
            print(f"║{' ' * (w - 2)}║")
            continue
        full = "".join(str(p) if isinstance(p, str) else p[0] for p in parts)
        rw = _render_width(full)
        pad = w - 2 - rw
        left = pad // 2
        right = pad - left
        print(f"║{' ' * left}{full}{' ' * right}║")
    print(f"╚{'═' * (w - 2)}╝")


def print_welcome(is_returning: bool):
    now = datetime.now()
    period = "早上" if now.hour < 12 else "下午" if now.hour < 18 else "晚上"

    if not is_returning:
        draw_box([
            [],
            [c("诸葛策 · 个人战略引擎", C.BOLD, C.CYAN)],
            [],
            [c("家庭 · 职场 · 创业 · 健康 · 财务 · 社交 · 学习 · 精神", C.DIM)],
            [],
            [c("试试这样聊", C.YELLOW)],
            [c("  → 先帮我建个档案")],
            [c("  → 分析我的人生状态")],
            [c("  → 今天有什么建议？")],
            [],
            [c(f"· {random_quote()}", C.DIM)],
            [],
            [c("输入 ", C.DIM), c("exit", C.YELLOW), c(" 退出", C.DIM)],
            [],
        ])
    else:
        profile = load_profile()
        name = profile.get("basic", {}).get("name", "朋友")
        raw_domains = profile.get("domains", {})
        active_goals = []
        if isinstance(raw_domains, dict):
            for d_val in raw_domains.values():
                if isinstance(d_val, dict):
                    active_goals.extend(d_val.get("goals", []))

        time_str = now.strftime("%Y-%m-%d %H:%M")

        rows = [
            [],
            [c("诸葛策 · 个人战略引擎", C.BOLD, C.CYAN)],
            [],
            [c(f"{period}好，", C.YELLOW), c(name, C.BOLD, C.MAGENTA), c(f"，现在是{time_str}", C.YELLOW)],
        ]

        # 每日摘要
        days = _days_since_last()
        pending = _pending_decisions_count()
        summary_parts = []
        if pending > 0:
            summary_parts.append(f"{pending} 个决策待跟进")
        if summary_parts:
            rows.append([])
            rows.append([c("  ".join(summary_parts), C.DIM)])

        if active_goals:
            rows.append([])
            rows.append([c("当前目标", C.YELLOW)])
            for g in active_goals[:3]:
                rows.append([c(f"  → {g}")])
        rows.append([])
        rows.append([c(f"  · {random_quote()}", C.DIM)])
        rows.append([])
        rows.append([c("输入 ", C.DIM), c("exit", C.YELLOW), c(" 退出", C.DIM)])
        rows.append([])
        draw_box(rows)


def cmd_profile():
    profile = load_profile()
    if profile:
        print(json.dumps(profile, ensure_ascii=False, indent=2))
    else:
        print("还没有你的档案，聊起来就会建立。")
    return 0


def cmd_reset():
    journal_file = get_user_dir() / "journal.jsonl"
    if journal_file.exists():
        journal_file.unlink()
    print(f"对话历史已重置（用户: {get_user()}），档案和决策记录还在。")
    return 0


def main():
    argv = sys.argv[1:]

    # 解析 --user <name>
    args = []
    i = 0
    while i < len(argv):
        if argv[i] == "--user" and i + 1 < len(argv):
            set_user(argv[i + 1])
            i += 2
        elif argv[i] == "--user":
            i += 1
        else:
            args.append(argv[i])
            i += 1

    # 从 data/ 迁移到 data/{user}/
    from memory import _migrate_from_root as _migrate
    _migrate()
    user_id = get_user()

    if args and args[0] == "--help":
        print("诸葛策 — 个人战略引擎")
        print()
        print("用法:")
        print("  source .venv/bin/activate                直接开始对话")
        print("  --profile                                查看档案")
        print("  --reset                                  重置对话历史")
        print(f"  --user <name>                           指定用户（默认 chenpeng）")
        return 0

    if args and args[0] == "--profile":
        return cmd_profile()
    if args and args[0] == "--reset":
        return cmd_reset()

    try:
        agent = MingYuanAgent()
    except ValueError as e:
        print(f"错误: {e}")
        print("去 .env 文件填入你的 API 密钥。")
        return 1

    is_returning = bool(load_profile())

    if args:
        user_input = " ".join(args)
        print_welcome(is_returning)
    else:
        print_welcome(is_returning)
        user_input = _read_input(f"{C.GREEN}你{C.RESET}: ")

    while True:
        if user_input.lower() in ("exit", "quit"):
            print()
            print(f"{C.BOLD}{C.CYAN}诸葛策:{C.RESET} 下次见。")
            break
        if not user_input:
            user_input = _read_input(f"{C.GREEN}你{C.RESET}: ")
            continue

        print(f"{C.CYAN}─── ─── ─── ─── ─── ─── ───{C.RESET}")

        # 进入思考模式：非规范 + 不回显，后台捕获键盘输入
        fd = sys.stdin.fileno()
        think_old = _term_thinking_mode(fd)
        buf = InputBuffer(fd)
        tool_step = 0
        spinner = Spinner(step=tool_step)
        buf.start()
        spinner.start()

        try:
            for msg_type, content in agent.chat(user_input):
                spinner.stop()
                _clear_line()
                safe = _sanitize(content)
                if msg_type == "text":
                    print(f"{C.BOLD}{C.CYAN}诸葛策:{C.RESET} {safe}")
                elif msg_type == "tool_start":
                    tool_step += 1
                    print(f"  {C.DIM}[{safe}]{C.RESET}")
                spinner = Spinner(step=tool_step)
                spinner.start()
            spinner.stop()
            _clear_line()
            print(f"{C.CYAN}─── ─── ─── ─── ─── ─── ───{C.RESET}")
        except KeyboardInterrupt:
            print()
            print(f"{C.BOLD}{C.CYAN}诸葛策:{C.RESET} 下次见。")
            break
        except Exception as e:
            print(f"\n  {C.DIM}错误: {e}{C.RESET}")
            return 1
        finally:
            spinner.stop()
            buf.stop()
            _restore_terminal(fd, think_old)
            _clear_line()

        # 检查思考期间用户是否有输入
        buffered = buf.get_and_clear()
        if buffered:
            if buffered.lower() in ("exit", "quit"):
                print()
                print(f"{C.BOLD}{C.CYAN}诸葛策:{C.RESET} 下次见。")
                break
            print(f"{C.GREEN}你{C.RESET}: {buffered}")
            user_input = buffered
            continue

        user_input = _read_input(f"{C.GREEN}你{C.RESET}: ")

    return 0


if __name__ == "__main__":
    sys.exit(main())
