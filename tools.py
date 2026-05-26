"""工具定义与处理函数"""

import json
import os
import re
import subprocess
from datetime import date, datetime
from pathlib import Path

from memory import save_profile, save_journal, save_decision, load_full_context, _sanitize
from bazi import calculate_bazi as _calc_bazi
from wisdom import search_wisdom as _search_wisdom, format_wisdom as _format_wisdom

TOOL_DEFINITIONS = [
    {
        "name": "save_memory",
        "description": "将信息保存到持久化记忆。用于存储用户画像更新、反思日记或决策记录。",
        "input_schema": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["profile", "journal", "decision"],
                    "description": "记忆类型：profile=更新用户画像，journal=记录日志/反思，decision=记录决策分析",
                },
                "data": {
                    "type": "object",
                    "description": "要保存的数据。profile时使用 domains/goals/values 等字段；journal时使用 content/tags 等；decision时使用 decision/analysis/options 等。",
                },
            },
            "required": ["type", "data"],
        },
    },
    {
        "name": "load_context",
        "description": "加载用户档案、近期日志和决策历史等持久化记忆，了解用户背景。每次对话开始时应该调用此工具。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "enum": ["all", "profile", "journal", "decisions"],
                    "description": "要加载的内容：all=全部加载，profile=仅档案，journal=仅日志，decisions=仅决策",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "analyze_situation",
        "description": "对用户当前人生状况进行结构化多维度分析，覆盖家庭、职场、创业、健康、财务、社交、学习、精神八个维度，识别强项、弱项、机会和风险。",
        "input_schema": {
            "type": "object",
            "properties": {
                "situation": {
                    "type": "string",
                    "description": "用户描述当前状况",
                }
            },
            "required": ["situation"],
        },
    },
    {
        "name": "analyze_decision",
        "description": "对重大决策进行系统化分析，包括 SWOT 分析、风险评估、对八个生活维度的影响分析，以及建议方案。",
        "input_schema": {
            "type": "object",
            "properties": {
                "decision": {
                    "type": "string",
                    "description": "需要做出的决策描述",
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选的方案列表",
                },
                "context": {
                    "type": "string",
                    "description": "决策的额外背景信息",
                },
            },
            "required": ["decision", "options"],
        },
    },
    {
        "name": "calculate_bazi",
        "description": "根据出生年月日时计算八字命盘，分析五行强弱喜忌，为职业选择和人生决策提供命理参考。如果用户不知道具体时辰，birth_hour 填 -1。",
        "input_schema": {
            "type": "object",
            "properties": {
                "birth_year": {
                    "type": "integer",
                    "description": "出生年份（公历）",
                },
                "birth_month": {
                    "type": "integer",
                    "description": "出生月份（公历，1-12）",
                },
                "birth_day": {
                    "type": "integer",
                    "description": "出生日期（公历，1-31）",
                },
                "birth_hour": {
                    "type": "integer",
                    "description": "出生时辰（0-23），不知道则填 -1",
                },
                "birth_minute": {
                    "type": "integer",
                    "description": "出生分钟（0-59），不知道则填 0",
                },
                "gender": {
                    "type": "string",
                    "enum": ["male", "female"],
                    "description": "性别",
                },
            },
            "required": ["birth_year", "birth_month", "birth_day"],
        },
    },
    {
        "name": "search_wisdom",
        "description": "检索成功人士的价值观、理念和行动指南。当你给用户建议时，可以查询相关人物的真实理念来丰富你的指导。支持按人名或领域搜索。",
        "input_schema": {
            "type": "object",
            "properties": {
                "figure": {
                    "type": "string",
                    "description": "人物名，如'马斯克''纳瓦尔''巴菲特'等。可选，留空则按领域搜索。",
                },
                "domain": {
                    "type": "string",
                    "description": "领域关键词，如'创业''投资''产品''幸福'等。可选，留空则按人物名搜索。",
                },
            },
        },
    },
    {
        "name": "render_page",
        "description": "将内容渲染为 HTML 页面并在浏览器打开。两种模式：(1) 传 content（markdown）自动套用精美模板（适合快节奏报告）；(2) 传 html_content（完整 HTML）原样渲染（适合惊艳展示——此时你应当遵循 frontend-design 理念，设计有独特审美的页面）。仅在内容有展示价值时使用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "页面标题",
                },
                "content": {
                    "type": "string",
                    "description": "Markdown 格式内容（与 html_content 二选一），支持 **粗体**、*斜体*、列表、表格、标题等",
                },
                "html_content": {
                    "type": "string",
                    "description": "完整 HTML 源码（与 content 二选一），提供此参数时原样输出到浏览器。请遵循 frontend-design 理念：选择鲜明美学方向、独特字体搭配、大胆配色排版，让页面令人印象深刻。",
                },
                "theme": {
                    "type": "string",
                    "enum": ["serene", "bold", "minimal"],
                    "description": "仅对 content（markdown）模式有效。serene=东方静谧暖调（默认），bold=深色高对比现代风，minimal=极简大量留白。html_content 模式不生效。",
                },
            },
            "required": ["title"],
        },
    },
]


def handle_save_memory(input_data: dict) -> str:
    mem_type = input_data["type"]
    data = input_data["data"]

    if not isinstance(data, dict):
        return f"保存失败：data 必须是对象，收到 {type(data).__name__}"

    if mem_type == "profile":
        save_profile(data)
        return "用户档案已更新并保存。"
    elif mem_type == "journal":
        if "date" not in data:
            data["date"] = date.today().isoformat()
        save_journal(data)
        return "日志已保存。"
    elif mem_type == "decision":
        if "date" not in data:
            data["date"] = date.today().isoformat()
        save_decision(data)
        return "决策记录已保存。"
    else:
        return f"未知记忆类型: {mem_type}"


def handle_load_context(input_data: dict) -> str:
    query = input_data.get("query", "all")
    ctx = load_full_context()

    parts = []
    if query in ("all", "profile") and ctx.get("profile"):
        parts.append(f"【用户档案】\n{json.dumps(ctx['profile'], ensure_ascii=False, indent=2)}")
    if query in ("all", "journal") and ctx.get("recent_journal"):
        parts.append(f"【近期日志（最近 {len(ctx['recent_journal'])} 条）】\n{json.dumps(ctx['recent_journal'], ensure_ascii=False, indent=2)}")
    if query in ("all", "decisions") and ctx.get("recent_decisions"):
        parts.append(f"【历史决策（最近 {len(ctx['recent_decisions'])} 条）】\n{json.dumps(ctx['recent_decisions'], ensure_ascii=False, indent=2)}")

    if not parts:
        return "暂无保存的用户记忆。这是一个新用户，请引导他建立个人档案。"

    return "\n\n".join(parts)


def handle_analyze_situation(input_data: dict) -> str:
    situation = input_data.get("situation", "")

    save_journal({
        "type": "situation_analysis_request",
        "content": situation,
        "date": date.today().isoformat(),
    })

    return f"""用户描述的情况：{situation}

请从八个维度逐项分析当前状况，识别每个维度的强项、弱项、机会和风险。
分析完成后，给出综合评估和优先建议。

**重要：分析完成后，请调用 save_memory(type=journal) 将完整的分析结论和重点建议保存到日志，以便后续回顾。**"""


def handle_analyze_decision(input_data: dict) -> str:
    decision = input_data.get("decision", "")
    options = input_data.get("options", [])
    context = input_data.get("context", "")

    save_decision({
        "decision": decision,
        "options": options,
        "context": context,
        "status": "analyzing",
    })

    return f"""需要分析的决策：{decision}
可选方案：{'，'.join(options)}
背景：{context}

请进行以下分析：
1. 每个方案的 SWOT 分析
2. 对各人生维度（家庭、职场、创业、健康、财务、社交、学习、精神）的潜在影响
3. 风险评估
4. 推荐方案及理由
5. 下一步行动建议

**重要：分析完成后，请调用 save_memory(type=decision) 将完整的分析结论存档，状态设为 analyzed。**"""


def handle_calculate_bazi(input_data: dict) -> str:
    """计算八字命盘并返回结构化结果"""
    from bazi import calculate_bazi as _calc

    result = _calc(
        birth_year=input_data["birth_year"],
        birth_month=input_data["birth_month"],
        birth_day=input_data["birth_day"],
        birth_hour=input_data.get("birth_hour", -1),
        birth_minute=input_data.get("birth_minute", 0),
        gender=input_data.get("gender"),
    )

    # 自动保存到 profile
    save_profile({
        "basic": {
            "birth_date": result["birth_date"],
            "gender": input_data.get("gender"),
        },
        "bazi": result,
    })

    return f"""```json
{json.dumps(result, ensure_ascii=False, indent=2)}
```

请基于以上八字命盘数据，结合用户的完整背景，给出命理解读和结合现实的建议："""


def handle_search_wisdom(input_data: dict) -> str:
    figure = input_data.get("figure", "")
    domain = input_data.get("domain", "")
    results = _search_wisdom(figure=figure, domain=domain)

    if not results:
        return f"未找到相关人物。可用人物：{'、'.join(e['name'] for e in _search_wisdom())}"

    if len(results) == 1:
        return _format_wisdom(results[0])
    else:
        lines = [f"找到 {len(results)} 位相关人物：", ""]
        for r in results:
            lines.append(f"- **{r['name']}**（{r['name_en']}）— {'、'.join(r['domains'][:3])}")
            lines.append(f"  {r['summary']}")
        lines.append("")
        lines.append("可输入具体人物名查看详细信息。")
        return "\n".join(lines)


# ── 简易 Markdown → HTML 转换 ──

def _md_to_html(text: str) -> str:
    """极简 Markdown 转 HTML（覆盖常用语法即可）。"""
    lines = text.split("\n")
    html = []
    in_table = False
    in_list = False
    in_ol = False

    def _inline(m: str) -> str:
        m = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", m)
        m = re.sub(r"\*(.+?)\*", r"<em>\1</em>", m)
        m = re.sub(r"`(.+?)`", r"<code>\1</code>", m)
        return m

    i = 0
    while i < len(lines):
        line = lines[i]

        # 表格
        if line.startswith("|") and line.endswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            tag = "th" if not in_table else "td"
            # 分隔行（|---|---|）
            if re.match(r"^[\s|:-]+$", line):
                in_table = True
                i += 1
                continue
            if not in_table:
                html.append("<table><thead><tr>")
                html.extend(f"<{tag}>{_inline(c)}</{tag}>" for c in cells)
                html.append("</tr></thead><tbody>")
                in_table = True
            else:
                html.append("<tr>")
                html.extend(f"<td>{_inline(c)}</td>" for c in cells)
                html.append("</tr>")
            i += 1
            # 表格结束检测
            if i >= len(lines) or not (lines[i].startswith("|") and lines[i].endswith("|")):
                html.append("</tbody></table>")
                in_table = False
            continue

        # 标题
        hm = re.match(r"^(#{1,3})\s+(.+)$", line)
        if hm:
            level = len(hm.group(1))
            html.append(f"<h{level}>{_inline(hm.group(2))}</h{level}>")
            i += 1
            continue

        # 无序列表
        lm = re.match(r"^[-*]\s+(.+)$", line)
        if lm:
            if not in_list:
                html.append("<ul>")
                in_list = True
            html.append(f"<li>{_inline(lm.group(1))}</li>")
            i += 1
            continue
        if in_list:
            html.append("</ul>")
            in_list = False

        # 有序列表
        om = re.match(r"^\d+\.\s+(.+)$", line)
        if om:
            if not in_ol:
                html.append("<ol>")
                in_ol = True
            html.append(f"<li>{_inline(om.group(1))}</li>")
            i += 1
            continue
        if in_ol:
            html.append("</ol>")
            in_ol = False

        # 引用
        qm = re.match(r"^>\s+(.+)$", line)
        if qm:
            html.append(f"<blockquote>{_inline(qm.group(1))}</blockquote>")
            i += 1
            continue

        # 分隔线
        if re.match(r"^---+\s*$", line):
            html.append("<hr>")
            i += 1
            continue

        # 空行 → 段落分隔
        if not line.strip():
            i += 1
            continue

        # 普通段落
        html.append(f"<p>{_inline(line)}</p>")
        i += 1

    if in_list:
        html.append("</ul>")
    if in_ol:
        html.append("</ol>")

    return "\n".join(html)


_THEMES = {
    "serene": r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} — 诸葛策</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@400;600;700&family=Noto+Sans+SC:wght@300;400;500&display=swap" rel="stylesheet">
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{
  font-family: "Noto Serif SC", "Noto Sans SC", serif;
  background: #f7f5f1;
  color: #2c2c2c;
  line-height: 1.9;
  padding: 3rem 1rem;
  background-image: radial-gradient(circle at 20% 50%, rgba(196,168,130,0.04) 0%, transparent 50%);
}}
.container {{ max-width:720px; margin:0 auto; background:rgba(255,255,255,0.85); backdrop-filter:blur(8px); border-radius:16px; padding:3rem 2.5rem; box-shadow:0 4px 40px rgba(0,0,0,0.04); border:1px solid rgba(0,0,0,0.04); }}
h1 {{ font-size:1.5rem; font-weight:700; letter-spacing:0.08em; margin-bottom:0.3rem; color:#1a1a1a; }}
h2 {{ font-size:1.15rem; font-weight:600; margin:2rem 0 0.8rem; color:#333; letter-spacing:0.04em; padding-bottom:0.3rem; border-bottom:1px solid #ede8e0; }}
h3 {{ font-size:1rem; font-weight:600; margin:1.4rem 0 0.5rem; color:#555; }}
p {{ margin:0.6rem 0; color:#555; line-height:1.9; }}
strong {{ color:#1a1a1a; font-weight:600; }}
em {{ color:#a88b6a; font-style:normal; }}
ul, ol {{ margin:0.5rem 0 0.5rem 1.2rem; }}
li {{ margin:0.3rem 0; color:#555; }}
blockquote {{ margin:1.2rem 0; padding:0.8rem 1.2rem; border-left:3px solid #c4a882; background:rgba(196,168,130,0.06); border-radius:0 8px 8px 0; color:#666; }}
code {{ background:#ede8e0; padding:0.15rem 0.4rem; border-radius:4px; font-size:0.9em; color:#8a7a6a; font-family:"SF Mono","Fira Code",monospace; }}
pre {{ background:#f5f3ef; padding:1.2rem; border-radius:10px; overflow-x:auto; font-size:0.85rem; margin:1rem 0; border:1px solid #ede8e0; }}
table {{ width:100%; border-collapse:collapse; margin:1.2rem 0; font-size:0.9rem; }}
th, td {{ padding:0.6rem 0.8rem; text-align:left; border-bottom:1px solid #ede8e0; }}
th {{ background:#faf8f5; font-weight:600; color:#333; }}
tr:last-child td {{ border-bottom:none; }}
hr {{ border:none; border-top:1px solid #e8e0d6; margin:1.8rem 0; }}
.meta {{ font-size:0.75rem; color:#aaa; margin-top:2.5rem; text-align:center; letter-spacing:0.05em; }}
@media (max-width:600px) {{ .container {{ padding:2rem 1.2rem; }} }}
</style>
</head>
<body>
<div class="container">
<div style="width:2rem;height:2px;background:#c4a882;margin-bottom:2rem;"></div>
{body}
<div class="meta">诸葛策 · {date}</div>
</div>
</body>
</html>""",

    "bold": r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} — 诸葛策</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;500;700;900&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{
  font-family: "Inter", -apple-system, sans-serif;
  background: #0f0f0f;
  color: #e8e8e8;
  line-height: 1.7;
  padding: 2rem 1rem;
  background-image: radial-gradient(ellipse at 80% 20%, rgba(233,69,96,0.06) 0%, transparent 50%), radial-gradient(ellipse at 20% 80%, rgba(78,84,200,0.06) 0%, transparent 50%);
}}
.container {{ max-width:780px; margin:0 auto; background:#1a1a1a; border-radius:12px; padding:3rem 2.5rem; border:1px solid #2a2a2a; box-shadow:0 8px 60px rgba(0,0,0,0.3); }}
h1 {{ font-size:1.6rem; font-weight:900; letter-spacing:-0.02em; color:#fff; margin-bottom:0.3rem; }}
h2 {{ font-size:1.2rem; font-weight:700; margin:2rem 0 0.8rem; color:#fff; letter-spacing:-0.01em; }}
h3 {{ font-size:1rem; font-weight:600; margin:1.4rem 0 0.5rem; color:#ccc; }}
p {{ margin:0.6rem 0; color:#a8a8a8; }}
strong {{ color:#fff; font-weight:600; }}
em {{ color:#e94560; font-style:normal; }}
ul, ol {{ margin:0.5rem 0 0.5rem 1.2rem; }}
li {{ margin:0.3rem 0; color:#a8a8a8; }}
blockquote {{ margin:1.2rem 0; padding:0.8rem 1.2rem; border-left:3px solid #e94560; background:rgba(233,69,96,0.06); border-radius:0 8px 8px 0; color:#999; }}
code {{ background:#2a2a2a; padding:0.15rem 0.4rem; border-radius:4px; font-size:0.9em; color:#e94560; font-family:"JetBrains Mono","SF Mono",monospace; }}
pre {{ background:#222; padding:1.2rem; border-radius:10px; overflow-x:auto; font-size:0.85rem; margin:1rem 0; border:1px solid #2a2a2a; }}
table {{ width:100%; border-collapse:collapse; margin:1.2rem 0; font-size:0.9rem; }}
th, td {{ padding:0.6rem 0.8rem; text-align:left; border-bottom:1px solid #2a2a2a; }}
th {{ background:#222; font-weight:600; color:#fff; }}
tr:last-child td {{ border-bottom:none; }}
hr {{ border:none; border-top:1px solid #2a2a2a; margin:1.8rem 0; }}
.meta {{ font-size:0.75rem; color:#555; margin-top:2.5rem; text-align:center; }}
@media (max-width:600px) {{ .container {{ padding:2rem 1.2rem; }} }}
</style>
</head>
<body>
<div class="container">
<div style="width:3rem;height:3px;background:linear-gradient(90deg,#e94560,#4e54c8);margin-bottom:2rem;border-radius:2px;"></div>
{body}
<div class="meta">诸葛策 · {date}</div>
</div>
</body>
</html>""",

    "minimal": r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} — 诸葛策</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@200;300;400;500&display=swap" rel="stylesheet">
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{
  font-family: "Noto Sans SC", -apple-system, sans-serif;
  background: #fff;
  color: #1a1a1a;
  line-height: 1.8;
  padding: 4rem 1rem;
}}
.container {{ max-width:680px; margin:0 auto; padding:0; }}
h1 {{ font-size:1.3rem; font-weight:200; letter-spacing:0.15em; color:#1a1a1a; margin-bottom:0.5rem; }}
h2 {{ font-size:1rem; font-weight:400; letter-spacing:0.1em; margin:2.5rem 0 0.8rem; color:#333; }}
h3 {{ font-size:0.9rem; font-weight:500; margin:1.5rem 0 0.5rem; color:#555; text-transform:uppercase; letter-spacing:0.08em; }}
p {{ margin:0.5rem 0; color:#666; font-weight:300; }}
strong {{ color:#1a1a1a; font-weight:400; }}
em {{ color:#999; font-style:normal; }}
ul, ol {{ margin:0.4rem 0 0.4rem 1rem; }}
li {{ margin:0.25rem 0; color:#666; font-weight:300; }}
blockquote {{ margin:1rem 0; padding:0.5rem 0 0.5rem 1.5rem; border-left:1px solid #ddd; color:#999; font-weight:300; }}
code {{ background:#f5f5f5; padding:0.1rem 0.3rem; border-radius:2px; font-size:0.85em; color:#666; font-family:"SF Mono",monospace; }}
pre {{ background:#fafafa; padding:1rem; border-radius:4px; overflow-x:auto; font-size:0.8rem; margin:1rem 0; }}
table {{ width:100%; border-collapse:collapse; margin:1rem 0; font-size:0.85rem; }}
th, td {{ padding:0.4rem 0.6rem; text-align:left; border-bottom:1px solid #eee; }}
th {{ font-weight:400; color:#333; }}
tr:last-child td {{ border-bottom:none; }}
hr {{ border:none; border-top:1px solid #eee; margin:2rem 0; }}
.meta {{ font-size:0.7rem; color:#ccc; margin-top:3rem; text-align:center; letter-spacing:0.1em; }}
@media (max-width:600px) {{ body {{ padding:2rem 1rem; }} }}
</style>
</head>
<body>
<div class="container">
{body}
<div class="meta">诸葛策 · {date}</div>
</div>
</body>
</html>""",
}


_THEME_KEYS = list(_THEMES.keys())


def handle_render_page(input_data: dict) -> str:
    title = input_data.get("title", "诸葛策报告")
    html_content = input_data.get("html_content")
    content = input_data.get("content", "")
    theme = input_data.get("theme", "serene")

    if html_content:
        html = html_content
    else:
        if theme not in _THEMES:
            theme = "serene"
        html = _THEMES[theme].format(
            title=title,
            body=_md_to_html(content),
            date=datetime.now().strftime("%Y-%m-%d %H:%M"),
        )

    renders_dir = Path(__file__).parent / "renders"
    renders_dir.mkdir(parents=True, exist_ok=True)

    safe_name = re.sub(r"[^\w一-鿿]", "_", title)
    safe_name = re.sub(r"_+", "_", safe_name).strip("_")[:40]
    filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_name}.html"
    filepath = renders_dir / filename

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)

    try:
        subprocess.run(["open", str(filepath)], check=False)
    except Exception:
        pass

    save_journal({
        "type": "render_page",
        "title": title,
        "file": str(filepath),
        "date": date.today().isoformat(),
    })

    return f"页面已打开: {filepath}"


TOOL_HANDLERS = {
    "save_memory": handle_save_memory,
    "load_context": handle_load_context,
    "analyze_situation": handle_analyze_situation,
    "analyze_decision": handle_analyze_decision,
    "calculate_bazi": handle_calculate_bazi,
    "search_wisdom": handle_search_wisdom,
    "render_page": handle_render_page,
}
