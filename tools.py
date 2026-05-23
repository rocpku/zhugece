"""工具定义与处理函数"""

import json
from datetime import date

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


TOOL_HANDLERS = {
    "save_memory": handle_save_memory,
    "load_context": handle_load_context,
    "analyze_situation": handle_analyze_situation,
    "analyze_decision": handle_analyze_decision,
    "calculate_bazi": handle_calculate_bazi,
    "search_wisdom": handle_search_wisdom,
}
