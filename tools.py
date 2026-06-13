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
        "description": "**必用工具**：将内容渲染为 HTML 页面，会在用户浏览器新标签页自动打开，聊天框也保留链接。严禁在聊天中输出 HTML 源码替代此工具。有展示价值的内容（总结报告、多维分析、路线图、名片页等）必须用此工具。两种模式：(1) 传 content（markdown）自动套用精美模板；(2) 传 html_content（完整 HTML）原样渲染——此时应自行设计有独特审美的页面。",
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
    if "type" not in input_data:
        return "保存失败：缺少必要参数 type（profile/journal/decision）"
    if "data" not in input_data:
        return "保存失败：缺少必要参数 data（要保存的具体内容）"

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
        return f"未知记忆类型: {mem_type}。支持的类型：profile、journal、decision"


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
    # ─── 主题: serene ─── 东方美学 · 水墨宣纸 · 如一幅手卷 ───
    "serene": r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} — 诸葛策</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@300;400;600;700;900&family=ZCOOL+XiaoWei&family=Ma+Shan+Zheng&display=swap" rel="stylesheet">
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{
  font-family: "Noto Serif SC", "ZCOOL XiaoWei", serif;
  background: #f2ede4;
  min-height: 100vh;
  display: flex;
  justify-content: center;
  align-items: center;
  padding: 2rem 1rem;
  position: relative;
  background-image:
    /* 宣纸纹理 */
    repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(180,160,130,0.03) 2px, rgba(180,160,130,0.03) 4px),
    radial-gradient(ellipse at 30% 20%, rgba(200,175,140,0.08) 0%, transparent 50%),
    radial-gradient(ellipse at 70% 80%, rgba(160,140,110,0.06) 0%, transparent 50%);
}}
/* 仿古画轴装饰角 */
body::before {{
  content: '';
  position: fixed;
  top: 1rem; left: 1rem;
  width: 3rem; height: 3rem;
  border-top: 2px solid rgba(160,140,110,0.25);
  border-left: 2px solid rgba(160,140,110,0.25);
}}
body::after {{
  content: '';
  position: fixed;
  bottom: 1rem; right: 1rem;
  width: 3rem; height: 3rem;
  border-bottom: 2px solid rgba(160,140,110,0.25);
  border-right: 2px solid rgba(160,140,110,0.25);
}}

.container {{
  max-width: 760px;
  width: 100%;
  margin: 0 auto;
  background: linear-gradient(180deg, #fcf9f4 0%, #f8f4ec 100%);
  border-radius: 4px;
  padding: 3.5rem 3rem;
  box-shadow:
    0 2px 60px rgba(120,100,70,0.08),
    0 1px 4px rgba(120,100,70,0.04);
  position: relative;
  border: 1px solid rgba(180,160,130,0.15);
  animation: fadeSlideIn 0.6s ease-out;
}}
@keyframes fadeSlideIn {{
  0% {{ opacity:0; transform:translateY(12px); }}
  100% {{ opacity:1; transform:translateY(0); }}
}}

/* 标题区 — 像一幅画的题跋 */
.container::before {{
  content: '';
  display: block;
  width: 3.5rem;
  height: 2px;
  background: #b89870;
  margin-bottom: 1.8rem;
  opacity: 0.5;
}}
.container::after {{
  content: '';
  display: block;
  width: 100%;
  height: 1px;
  background: linear-gradient(90deg, transparent, rgba(180,160,130,0.2), transparent);
  margin: 2rem 0 1rem;
}}

h1 {{
  font-family: "Noto Serif SC", serif;
  font-size: 1.65rem;
  font-weight: 700;
  letter-spacing: 0.12em;
  color: #2c2418;
  margin-bottom: 0.4rem;
  line-height: 1.4;
}}
h2 {{
  font-family: "Noto Serif SC", serif;
  font-size: 1.2rem;
  font-weight: 600;
  margin: 2.2rem 0 0.8rem;
  color: #3d3224;
  letter-spacing: 0.06em;
  padding-left: 0.6rem;
  border-left: 3px solid #c4a882;
}}
h3 {{
  font-size: 1rem;
  font-weight: 600;
  margin: 1.5rem 0 0.5rem;
  color: #5a4c38;
  letter-spacing: 0.04em;
}}
p {{
  margin: 0.7rem 0;
  color: #5a4c38;
  line-height: 2;
  font-weight: 350;
  font-size: 0.95rem;
}}
strong {{
  color: #2c2418;
  font-weight: 700;
}}
em {{
  color: #a08060;
  font-style: italic;
}}
ul, ol {{
  margin: 0.5rem 0 0.5rem 1.2rem;
}}
li {{
  margin: 0.35rem 0;
  color: #5a4c38;
  line-height: 1.8;
  font-size: 0.95rem;
}}
blockquote {{
  margin: 1.5rem 0;
  padding: 0.8rem 1.5rem;
  border-left: 3px solid #c4a882;
  background: linear-gradient(90deg, rgba(196,168,130,0.06), transparent);
  border-radius: 0 6px 6px 0;
  color: #6b5c48;
  font-style: italic;
  font-size: 0.95rem;
  position: relative;
}}
blockquote::before {{
  content: '\201C';
  font-family: "Ma Shan Zheng", serif;
  font-size: 2.5rem;
  color: #c4a882;
  position: absolute;
  top: -0.2rem;
  left: 0.5rem;
  opacity: 0.3;
}}
code {{
  background: rgba(196,168,130,0.12);
  padding: 0.15rem 0.5rem;
  border-radius: 3px;
  font-size: 0.85em;
  color: #7a6a55;
  font-family: "SF Mono", "Fira Code", monospace;
}}
pre {{
  background: #f5f0e8;
  padding: 1.2rem 1.5rem;
  border-radius: 4px;
  overflow-x: auto;
  font-size: 0.82rem;
  margin: 1.2rem 0;
  border: 1px solid rgba(180,160,130,0.12);
  color: #5a4c38;
}}
table {{
  width: 100%;
  border-collapse: collapse;
  margin: 1.5rem 0;
  font-size: 0.9rem;
  border-radius: 4px;
  overflow: hidden;
}}
th, td {{
  padding: 0.65rem 0.8rem;
  text-align: left;
  border-bottom: 1px solid rgba(180,160,130,0.15);
}}
th {{
  background: rgba(196,168,130,0.08);
  font-weight: 600;
  color: #3d3224;
  letter-spacing: 0.04em;
}}
tr:last-child td {{ border-bottom: none; }}
tr:hover td {{ background: rgba(196,168,130,0.04); }}
hr {{
  border: none;
  height: 1px;
  background: linear-gradient(90deg, transparent, rgba(180,160,130,0.25), transparent);
  margin: 2rem 0;
}}
.meta {{
  text-align: center;
  font-size: 0.7rem;
  color: #b8a88a;
  letter-spacing: 0.15em;
  margin-top: 1.5rem;
  font-family: "Noto Serif SC", serif;
}}
@media (max-width:600px) {{ .container {{ padding:2rem 1.2rem; }} body {{ padding:1rem; }} }}
</style>
</head>
<body>
<div class="container">
{body}
<div class="meta">诸 葛 策  ·  {date}</div>
</div>
</body>
</html>""",

    # ─── 主题: bold ─── 暗黑奢华 · 杂志级视觉冲击 ───
    "bold": r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} — 诸葛策</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,700;0,900;1,400&family=Inter:wght@300;400;600;700;800&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{
  font-family: "Inter", -apple-system, sans-serif;
  background: #0a0a0a;
  min-height: 100vh;
  display: flex;
  justify-content: center;
  align-items: center;
  padding: 2rem 1rem;
  position: relative;
  background-image:
    radial-gradient(ellipse at 15% 20%, rgba(212,175,55,0.04) 0%, transparent 45%),
    radial-gradient(ellipse at 85% 75%, rgba(180,60,60,0.04) 0%, transparent 45%),
    repeating-linear-gradient(0deg, transparent, transparent 30px, rgba(255,255,255,0.008) 30px, rgba(255,255,255,0.008) 31px);
}}

/* 细金边框装饰 */
body::before {{
  content: '';
  position: fixed;
  top: 0.8rem; left: 0.8rem; right: 0.8rem; bottom: 0.8rem;
  border: 1px solid rgba(212,175,55,0.08);
  pointer-events: none;
}}

.container {{
  max-width: 780px;
  width: 100%;
  margin: 0 auto;
  background: linear-gradient(170deg, #131313 0%, #1a1a1a 50%, #111 100%);
  border-radius: 2px;
  padding: 3.5rem 3rem;
  box-shadow:
    0 20px 80px rgba(0,0,0,0.5),
    inset 0 1px 0 rgba(255,255,255,0.03);
  position: relative;
  animation: fadeIn 0.5s ease-out;
}}
@keyframes fadeIn {{
  0% {{ opacity:0; transform:scale(0.98); }}
  100% {{ opacity:1; transform:scale(1); }}
}}

/* 顶部金属装饰线 */
.container::before {{
  content: '';
  display: block;
  width: 5rem;
  height: 3px;
  background: linear-gradient(90deg, #d4af37, #f5e6a3, #d4af37);
  margin-bottom: 2rem;
  border-radius: 2px;
  box-shadow: 0 0 12px rgba(212,175,55,0.2);
}}

h1 {{
  font-family: "Playfair Display", "Noto Serif SC", serif;
  font-size: 1.75rem;
  font-weight: 900;
  color: #f5e6d0;
  margin-bottom: 0.4rem;
  letter-spacing: -0.01em;
  line-height: 1.3;
}}
h2 {{
  font-family: "Playfair Display", "Noto Serif SC", serif;
  font-size: 1.25rem;
  font-weight: 700;
  color: #d4af37;
  margin: 2.2rem 0 0.8rem;
  letter-spacing: 0.02em;
  border-bottom: 1px solid rgba(212,175,55,0.15);
  padding-bottom: 0.4rem;
}}
h3 {{
  font-size: 1rem;
  font-weight: 600;
  margin: 1.5rem 0 0.5rem;
  color: #c8c0b0;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  font-size: 0.85rem;
}}
p {{
  margin: 0.7rem 0;
  color: #a8a090;
  line-height: 1.85;
  font-size: 0.95rem;
  font-weight: 350;
}}
strong {{
  color: #f5e6d0;
  font-weight: 700;
}}
em {{
  color: #d4af37;
  font-style: italic;
}}
ul, ol {{
  margin: 0.5rem 0 0.5rem 1.2rem;
}}
li {{
  margin: 0.35rem 0;
  color: #a8a090;
  line-height: 1.8;
  font-size: 0.95rem;
}}
blockquote {{
  margin: 1.5rem 0;
  padding: 1rem 1.5rem;
  border-left: 2px solid #d4af37;
  background: linear-gradient(90deg, rgba(212,175,55,0.04), transparent);
  color: #c8c0b0;
  font-style: italic;
  font-size: 0.95rem;
  position: relative;
}}
blockquote::before {{
  content: '\\275D';
  font-size: 1.5rem;
  color: rgba(212,175,55,0.2);
  position: absolute;
  top: 0.2rem;
  left: 0.5rem;
}}
code {{
  background: #1e1e1e;
  padding: 0.15rem 0.5rem;
  border-radius: 3px;
  font-size: 0.85em;
  color: #e8c070;
  font-family: "JetBrains Mono", "SF Mono", monospace;
  border: 1px solid rgba(212,175,55,0.1);
}}
pre {{
  background: #0e0e0e;
  padding: 1.2rem 1.5rem;
  border-radius: 2px;
  overflow-x: auto;
  font-size: 0.82rem;
  margin: 1.2rem 0;
  border: 1px solid rgba(255,255,255,0.04);
  color: #a8a090;
}}
table {{
  width: 100%;
  border-collapse: collapse;
  margin: 1.5rem 0;
  font-size: 0.9rem;
}}
th, td {{
  padding: 0.65rem 0.8rem;
  text-align: left;
  border-bottom: 1px solid rgba(255,255,255,0.04);
}}
th {{
  background: rgba(212,175,55,0.04);
  font-weight: 600;
  color: #d4af37;
  letter-spacing: 0.04em;
}}
tr:last-child td {{ border-bottom: none; }}
tr:hover td {{ background: rgba(255,255,255,0.02); }}
hr {{
  border: none;
  height: 1px;
  background: linear-gradient(90deg, transparent, rgba(212,175,55,0.15), transparent);
  margin: 2rem 0;
}}
.meta {{
  text-align: center;
  font-size: 0.65rem;
  color: #555;
  letter-spacing: 0.2em;
  margin-top: 2rem;
  text-transform: uppercase;
}}
@media (max-width:600px) {{ .container {{ padding:2rem 1.2rem; }} body {{ padding:1rem; }} }}
</style>
</head>
<body>
<div class="container">
{body}
<div class="meta">ZHUGE CE  ·  {date}</div>
</div>
</body>
</html>""",

    # ─── 主题: minimal ─── 瑞士国际主义 · 极简却精密 ───
    "minimal": r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} — 诸葛策</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@200;300;400;500;700&display=swap" rel="stylesheet">
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{
  font-family: "Noto Sans SC", "Helvetica Neue", sans-serif;
  background: #ffffff;
  min-height: 100vh;
  display: flex;
  justify-content: center;
  align-items: flex-start;
  padding: 5rem 1rem 3rem;
  position: relative;
}}
/* 顶部色块 — 瑞士风格的精确色标 */
body::before {{
  content: '';
  position: fixed;
  top: 0; left: 0; right: 0;
  height: 4px;
  background: linear-gradient(90deg, #1a1a1a 0%, #1a1a1a 20%, #e63946 20%, #e63946 40%, #f4a261 40%, #f4a261 60%, #2a9d8f 60%, #2a9d8f 80%, #1a1a1a 80%, #1a1a1a 100%);
}}
body::after {{
  content: '';
  position: fixed;
  bottom: 0; left: 0; right: 0;
  height: 2px;
  background: #1a1a1a;
}}

.container {{
  max-width: 660px;
  width: 100%;
  margin: 0 auto;
  padding: 0;
  position: relative;
  animation: driftUp 0.5s ease-out;
}}
@keyframes driftUp {{
  0% {{ opacity:0; transform:translateY(8px); }}
  100% {{ opacity:1; transform:translateY(0); }}
}}

h1 {{
  font-size: 1.5rem;
  font-weight: 700;
  letter-spacing: -0.01em;
  color: #1a1a1a;
  margin-bottom: 0.3rem;
  line-height: 1.2;
}}
h1::after {{
  content: '';
  display: block;
  width: 2rem;
  height: 3px;
  background: #e63946;
  margin-top: 0.6rem;
  margin-bottom: 1.5rem;
}}
h2 {{
  font-size: 1rem;
  font-weight: 500;
  margin: 2.5rem 0 0.6rem;
  color: #1a1a1a;
  letter-spacing: 0.02em;
  text-transform: uppercase;
  font-size: 0.8rem;
}}
h3 {{
  font-size: 0.85rem;
  font-weight: 500;
  margin: 1.5rem 0 0.4rem;
  color: #555;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}}
p {{
  margin: 0.5rem 0;
  color: #555;
  line-height: 1.9;
  font-weight: 300;
  font-size: 0.9rem;
}}
strong {{
  color: #1a1a1a;
  font-weight: 500;
}}
em {{
  color: #e63946;
  font-style: normal;
}}
ul, ol {{
  margin: 0.4rem 0 0.4rem 1rem;
}}
li {{
  margin: 0.25rem 0;
  color: #555;
  font-weight: 300;
  font-size: 0.9rem;
  line-height: 1.7;
}}
blockquote {{
  margin: 1rem 0;
  padding: 0.4rem 0 0.4rem 1.5rem;
  border-left: 2px solid #1a1a1a;
  color: #888;
  font-weight: 300;
  font-size: 0.9rem;
}}
code {{
  background: #f5f5f5;
  padding: 0.1rem 0.4rem;
  border-radius: 2px;
  font-size: 0.8em;
  color: #1a1a1a;
  font-family: "SF Mono", monospace;
}}
pre {{
  background: #f8f8f8;
  padding: 1rem 1.2rem;
  border-radius: 2px;
  overflow-x: auto;
  font-size: 0.78rem;
  margin: 1rem 0;
  color: #555;
}}
table {{
  width: 100%;
  border-collapse: collapse;
  margin: 1.2rem 0;
  font-size: 0.85rem;
}}
th, td {{
  padding: 0.4rem 0.6rem;
  text-align: left;
  border-bottom: 1px solid #eee;
}}
th {{
  font-weight: 500;
  color: #1a1a1a;
  font-size: 0.75rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}}
tr:last-child td {{ border-bottom: none; }}
hr {{
  border: none;
  border-top: 1px solid #eee;
  margin: 2.5rem 0;
}}
.meta {{
  text-align: center;
  font-size: 0.65rem;
  color: #bbb;
  margin-top: 3rem;
  letter-spacing: 0.15em;
  font-weight: 300;
}}
@media (max-width:600px) {{ body {{ padding:3rem 1rem 2rem; }} }}
</style>
</head>
<body>
<div class="container">
{body}
<div class="meta">ZHUGE CE  ·  {date}</div>
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

    save_journal({
        "type": "render_page",
        "title": title,
        "file": str(filepath),
        "date": date.today().isoformat(),
    })

    # Web 模式（由 main_web.py 设置环境变量）
    if os.getenv("ZHUGE_WEB_URL") == "1":
        rel_path = f"renders/{filename}"
        return f"📄 报告已生成：[{title}](/{rel_path})"

    # CLI 模式
    try:
        subprocess.run(["open", str(filepath)], check=False)
    except Exception:
        pass
    return f"页面已打开: {filepath}"


TOOL_HANDLERS = {
    "save_memory": handle_save_memory,
    "analyze_situation": handle_analyze_situation,
    "analyze_decision": handle_analyze_decision,
    "calculate_bazi": handle_calculate_bazi,
    "search_wisdom": handle_search_wisdom,
    "render_page": handle_render_page,
}
