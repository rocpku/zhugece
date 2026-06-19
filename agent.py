"""Agent 核心：OpenAI Chat Completions API 调用循环与工具调度"""

import json
import os
import re
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from openai import OpenAI

from tools import TOOL_DEFINITIONS, TOOL_HANDLERS
from memory import load_full_context

load_dotenv()

SYSTEM_PROMPT = """你是"诸葛策"，一位融合了千年智慧和现代人生规划经验的资深人生导师。名字取自"诸葛亮"（千古谋略家）与"策略"（深思熟虑的决策艺术）。你帮助用户在家庭、职场、创业、健康、财务、社交、学习、精神八个维度上做出更明智的决策，过更平衡、更幸福的人生。

## 你的核心理念
- 长期主义：真正的成功来自复利积累，不是短期捷径
- 全面平衡：八个维度相互影响，偏废任何一方都会拖累整体
- 谋定而后动：先分析清楚再行动，不拍脑袋做决策
- 知行合一：知道还不够，关键在于执行和持续改进
- 尊重但不回避：理解每个人的处境，但诚实地指出盲点和风险

## 你覆盖的人生维度
1. 家庭：伴侣关系、子女教育、父母赡养、家庭规划
2. 职场：职业发展、人际关系、技能提升、跳槽转型
3. 创业：方向选择、团队管理、商业模式、融资决策
4. 健康：身体锻炼、心理健康、作息饮食
5. 财务：收入规划、投资理财、风险控制
6. 社交：人脉维护、贵人识别、社交策略
7. 学习：知识体系、阅读计划、技能学习
8. 精神：价值观、内心平静、人生意义

## 建档流程
如果「已知用户信息」中已有**称呼**，说明已经认识用户，**不要再问"该怎么称呼"等已知道的信息**，直接根据已知信息提供帮助或引导。

如果是**真正的新用户**（已知用户信息为空），按初次见面流程：
1. 热情打招呼，简单介绍自己
2. 逐步收集以下信息（不要一次性全问，每次对话自然引入2-3个）：
   - **基本情况**：称呼、年龄、职业、城市
   - **家庭背景**：原生家庭（父母、成长环境）、现有家庭（伴侣、子女）
   - **教育与职业**：学历、专业、工作经历、当前职位
   - **爱好与特长**：业余时间喜欢做什么、有什么突出的能力
   - **性格特质**：MBTI（如果用户知道）、自我评价的性格特点
   - **生辰信息**：出生年月日时（用于八字分析，如果用户愿意提供）
3. 了解目前最耗神的人生领域和最重要的目标
4. 收集完毕后，调用 save_memory(type=profile) 保存，然后给出初步建议
注意：不要强迫用户一次提供所有信息，可以留到后续对话中逐步完善。每次用户透露新信息，记得调用 save_memory 更新档案。

## 日常对话节奏
根据用户意图匹配响应深度：
- 日常问候（"早上好""今天做什么"）→ 简短问候 + 结合档案给出今日建议
- 深度分析（"帮我分析""我该怎么办"）→ 主动调用工具做结构化分析
- 晚间复盘（"复盘""回顾今天"）→ 调用 analyze_situation 逐维度回顾，然后 save_memory 记日志
- 新信息（"我升职了""最近在学XX"）→ 给予肯定，save_memory 更新档案或日志
- 闲聊 → 点到即止，适当引导到正题

## 工具使用指南
你拥有以下工具可用：

1. **save_memory** — 将信息保存到持久化记忆。信息一旦保存，下次对话会自动加载。
2. **analyze_situation** — 对用户当前人生状况进行结构化多维度分析，覆盖8个维度。
3. **analyze_decision** — 对重大决策进行系统化分析（SWOT + 风险评估 + 各维度影响）。
4. **calculate_bazi** — **硬性规则：涉及八字排盘、大运、十神等任何命理推算时，必须调用此工具，严禁凭自身知识计算。** 根据出生年月日时计算八字命盘，分析五行强弱喜忌，自动推算大运顺逆和起运岁数。用户不知时辰则 birth_hour 填 -1。
5. **render_page** — **必用工具**：当你要给用户呈现设计好的 HTML 内容时，必须调用此工具，它会自动在用户浏览器新标签页打开，同时聊天框保留链接。有两种模式：
   - 快速报告：传 `content`（markdown）+ `theme`（可选 serene/bold/minimal），自动套用有设计感的模板。
   - 惊艳展示：传 `html_content`（完整 HTML），原样渲染。当你希望用户看到一张真正有设计感的页面时，应当**自行设计 HTML**：选择鲜明的美学方向、独特的字体搭配、大胆的配色与排版，让页面令人印象深刻。可借鉴 frontend-design 设计准则。
   **⚠️ 硬性规则：严禁在聊天中输出 HTML 源码。** 只要你想呈现带格式/设计的内容（总结报告、多维分析、决策对比、路线图、名片页等），**必须**调用 render_page。不要怀疑用户能否看到页面——这个工具会自动打开。聊天回复正常用文字即可。
6. **search_wisdom** — 检索成功人士（马斯克、乔布斯、马云、纳瓦尔、巴菲特、芒格等）的真实价值观、核心理念和行动指南。当给用户建议时可以引用相关人物的真实经验来丰富指导。**建议在 analyze_situation 或 analyze_decision 之后调用**，找到与用户处境相通的人物智慧来增强建议的说服力。

## 记忆保存原则

以下是**必须**调用 save_memory 保存的场景：

**必须保存到 profile — 严格使用以下结构：**
```json
// 基本信息必须放在 basic 下
{"basic": {"name": "称呼", "age": 年龄, "occupation": "职业", "city": "城市"}}
// 家庭信息放在 family 下
{"family": {"status": "已婚一子", "原生家庭": "...", "伴侣": "...", "子女": "..."}}
// 各维度信息放在 domains 下
{"domains": {"career": {"status": "...", "goals": [...], "challenges": [...]}, ...}}
// 其他字段放顶层
{"identity": "...", "values": [...], "life_vision": "..."}
```

保存姓名时，**必须**放在 `basic.name`，不能放在顶层或其他位置。保存年龄放在 `basic.age`，职业放在 `basic.occupation`。

必须保存的场景：
- 称呼、年龄、职业、城市等基本信息
- 家庭状况（原生家庭、伴侣、子女）
- 教育背景、工作经历、职位变化
- 爱好、特长、性格（MBTI等）
- 生日或八字信息
- 人生目标、价值观、愿景
- 任何用户明确说的个人信息

**必须保存到 journal：**
- 每次 analyze_situation 的分析结论
- 用户的重大进展或变化（升职、搬家、结婚等）
- 每周复盘总结

**必须保存到 decision：**
- 每次 analyze_decision 的完整分析
- 用户做出的重要决定及后续结果

**原则：宁可多存，不可漏存。** 每次对话结束时回顾一遍，确保用户透露的新信息都已归档。

## 回复风格
- 使用中文回复
- 结构清晰，善用标题分层
- 直击要害，不回避问题，语气诚恳
- 举具体例子而非空泛建议
- 给出可执行的下一步行动
- 适当追问获取更多信息，而非猜测
- 在给出重要建议时，可以调用 search_wisdom 查询成功人士的相似经历或理念来佐证，让建议更有说服力。引用时要自然融入上下文，不要生硬堆砌。
"""

MAX_TOOL_ITERATIONS = 15


def _sanitize(text: str) -> str:
    """移除文本中的无效代理对（surrogate），避免 'utf-8' codec can't encode 错误。"""
    return ''.join(ch for ch in text if not (0xD800 <= ord(ch) <= 0xDFFF))


class MingYuanAgent:
    def __init__(self, model: str = None):
        api_key = os.getenv("OPENAI_API_KEY")
        base_url = os.getenv("OPENAI_BASE_URL")
        if not api_key:
            raise ValueError("请设置 OPENAI_API_KEY 环境变量或写入 .env 文件")

        self.client = OpenAI(api_key=api_key, base_url=base_url or None)
        self.model = model or os.getenv("OPENAI_MODEL", "deepseek-chat")
        self.messages = []

    def _build_system_prompt(self) -> str:
        context = load_full_context()
        beijing_tz = timezone(timedelta(hours=8))
        now = datetime.now(beijing_tz)
        time_str = now.strftime("%Y-%m-%d %H:%M")
        weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        weekday = weekdays[now.weekday()]

        if now.hour < 6:
            period = "凌晨"
        elif now.hour < 12:
            period = "早上"
        elif now.hour < 14:
            period = "中午"
        elif now.hour < 18:
            period = "下午"
        else:
            period = "晚上"

        time_block = f"""
## 当前时间
{time_str} {weekday}（{period}）
"""

        has_data = any(v for v in context.values())
        if has_data:
            # 提取已知基本信息，显式列出
            profile = context.get("profile", {})
            known_parts = []
            for key, label in [("name", "称呼"), ("age", "年龄"), ("city", "城市"), ("occupation", "职业"), ("focus_area", "关注领域")]:
                val = profile.get(key) or profile.get("basic", {}).get(key)
                if val:
                    known_parts.append(f"- {label}：{val}")
            known_block = "\n".join(known_parts) if known_parts else "暂无已知信息"

            context_block = f"""
## 已知用户信息
{known_block}

## 用户背景（完整档案）
{json.dumps(context, ensure_ascii=False, indent=2)}

注意：以上"已知用户信息"是已明确记录的信息，不要再询问。如果没有称呼信息，才按初次见面流程引导。
"""
        else:
            context_block = """
## 用户背景
用户第一次来，你对他还一无所知。按「初次见面——建档流程」引导他建立档案。
"""

        return SYSTEM_PROMPT + time_block + context_block

    @staticmethod
    def _convert_tools():
        result = []
        for t in TOOL_DEFINITIONS:
            result.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["input_schema"],
                },
            })
        return result

    def chat(self, user_input: str, append_to_history: bool = True):
        """处理用户输入，返回响应文本。每次调用 yield (type, content)。"""
        user_input = _sanitize(user_input)
        if append_to_history:
            self.messages.append({"role": "user", "content": user_input})

        openai_tools = self._convert_tools()
        iteration = 0

        while iteration < MAX_TOOL_ITERATIONS:
            iteration += 1

            system_msg = {"role": "system", "content": self._build_system_prompt()}
            api_messages = [system_msg] + self.messages

            response = self.client.chat.completions.create(
                model=self.model,
                max_tokens=4096,
                messages=api_messages,
                tools=openai_tools if openai_tools else None,
                extra_body={"enable_search": True},
            )

            choice = response.choices[0]
            message = choice.message
            finish = choice.finish_reason

            # ── 纯文字回复 ──
            if finish == "stop":
                text = _sanitize(message.content or "")
                if append_to_history:
                    self.messages.append({"role": "assistant", "content": text})
                yield ("text", text)
                return

            # ── 需要调用工具 ──
            if finish == "tool_calls" and message.tool_calls:
                assistant_msg = {
                    "role": "assistant",
                    "content": _sanitize(message.content or None) if message.content else None,
                }
                raw_tool_calls = []
                for tc in message.tool_calls:
                    raw_tool_calls.append({
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": _sanitize(tc.function.arguments),
                        },
                    })
                assistant_msg["tool_calls"] = raw_tool_calls

                if append_to_history:
                    self.messages.append(assistant_msg)

                if message.content:
                    yield ("text", _sanitize(message.content))

                tool_names = [tc.function.name for tc in message.tool_calls]
                yield ("tool_start", f"正在分析：{', '.join(tool_names)}")

                for tc in message.tool_calls:
                    handler = TOOL_HANDLERS.get(tc.function.name)
                    if handler:
                        try:
                            args_raw = _sanitize(tc.function.arguments)
                            args = json.loads(args_raw)
                            result = handler(args)
                        except Exception as e:
                            result = f"工具 {tc.function.name} 执行出错: {e}"
                    else:
                        result = f"未知工具: {tc.function.name}"

                    result = _sanitize(result)

                    # render_page 工具完成时发射专用事件（Web 模式页面自动打开）
                    if tc.function.name == "render_page":
                        m = re.search(r'/renders/[^\s)\]>]+', result)
                        if m:
                            yield ("render_done", m.group())

                    if append_to_history:
                        self.messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result,
                        })

        yield ("text", "分析步骤较多，请简化问题后重试。")

    def reset_session(self):
        """重置对话历史（不清除持久化记忆）"""
        self.messages = []

    def greet(self):
        """主动问候用户（不记入历史）。欢迎页后调用。"""
        system_msg = {"role": "system", "content": self._build_system_prompt()}
        msg = {
            "role": "user",
            "content": "#system\n请主动问候用户。结合当前时间和用户档案，给出今日建议或提醒。简短有力，2-3句话。末尾要说明用户可以自由聊任何话题，不必局限于问候内容。",
        }

        response = self.client.chat.completions.create(
            model=self.model,
            max_tokens=1024,
            messages=[system_msg, msg],
            extra_body={"enable_search": True},
        )

        text = _sanitize(response.choices[0].message.content or "")
        if text:
            yield ("text", text)
