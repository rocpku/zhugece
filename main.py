#!/usr/bin/env python3
"""诸葛幸福 — 人生发展 Agent CLI"""

import sys
import json
import re
import random
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from agent import MingYuanAgent
from memory import load_profile, DATA_DIR


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
        time_str = now.strftime("%Y-%m-%d %H:%M")
        draw_box([
            [],
            [c("诸葛幸福 ", C.BOLD, C.CYAN), c("· 人生发展导师", C.YELLOW)],
            [c(time_str, C.DIM)],
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
            [c("诸葛幸福 ", C.BOLD, C.CYAN),
             c(f"· {period}好，", C.YELLOW),
             c(name, C.BOLD, C.MAGENTA)],
            [c(time_str, C.DIM)],
        ]

        # 每日摘要
        days = _days_since_last()
        pending = _pending_decisions_count()
        summary_parts = []
        if days is not None:
            summary_parts.append(f"上次来访: {days} 天前")
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
    journal_file = DATA_DIR / "journal.jsonl"
    if journal_file.exists():
        journal_file.unlink()
    print("对话历史已重置，你的档案和决策记录还在。")
    return 0


def main():
    args = sys.argv[1:]

    if args and args[0] == "--help":
        print("诸葛幸福 — 人生发展导师")
        print()
        print("用法:")
        print("  source .venv/bin/activate    直接开始对话")
        print("  --profile                     查看档案")
        print("  --reset                       重置对话历史")
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
        user_input = input("你: ").strip()

    while True:
        if user_input.lower() in ("exit", "quit"):
            print()
            print(f"  {C.BOLD}{C.CYAN}诸葛幸福:{C.RESET} 下次见。")
            break
        if not user_input:
            user_input = input(f"  {C.GREEN}你{C.RESET}: ").strip()
            continue

        try:
            for msg_type, content in agent.chat(user_input):
                if msg_type == "text":
                    print(f"  {C.BOLD}{C.CYAN}诸葛幸福:{C.RESET} {content}")
                elif msg_type == "tool_start":
                    print(f"  {C.DIM}[{content}]{C.RESET}")
        except KeyboardInterrupt:
            print()
            print(f"  {C.BOLD}{C.CYAN}诸葛幸福:{C.RESET} 下次见。")
            break
        except Exception as e:
            print(f"\n  {C.DIM}错误: {e}{C.RESET}")
            return 1

        print()
        user_input = input(f"  {C.GREEN}你{C.RESET}: ").strip()

    return 0


if __name__ == "__main__":
    sys.exit(main())
