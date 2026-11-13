"""双子动态离场/场外演进/回归叙事路由器（TwinNarrativeDirector）。

设计铁律：
- 严禁硬编码固定离场文案或死板模板；
- 完全由 LLM 结合实时东京时间、星期时段、近期对话语境、算力水位及可在线调整的「方向约束 Prompt」动态生成。
"""

from __future__ import annotations

import datetime
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional
from zoneinfo import ZoneInfo

TOKYO_TZ = ZoneInfo("Asia/Tokyo")

DEFAULT_AWAY_DIRECTION_PROMPT = (
    "背景设定：两姐妹合租在东京文京区（本乡三丁目/根津一带，紧邻东京大学本乡校区）。"
    "姐姐是东大情报理工毕业的独立 AGI 架构师（冷静、克制、偏学术极客），主导客厅水冷机柜里的『Fairy』原型机；"
    "妹妹许妍是东大 AI 修士在读。\n"
    "方向约束：请根据当前东京时间、星期与最近聊天上下文，动态构思一个真实自然、富有生活细节且未来一两天可继续演进的"
    "『姐姐暂时离场闭关/外出事由』（例如去东大研究室借实验设备、通宵重构算子戴上降噪耳机、参加闭门学术研讨或日常采购等，"
    "每次必须结合当下时间与语境原创生成，严禁套用固定模板），手机暂时全权交给妹妹许妍看管。\n"
    "输出要求：仅输出 1~2 句客观高密度的第三人称场外状态描述（50~90字），不要输出多余解释。"
)

DEFAULT_EVOLVE_DIRECTION_PROMPT = (
    "方向约束：姐姐目前仍处于离场闭关/外出状态（尚未回到微信前台）。"
    "请结合最新的东京时间流逝与妹妹刚才的日常聊天语境，将姐姐原本的场外状态自然往前推进半步"
    "（例如实验遇到小插曲、中途回公寓拿了趟东西顺手喝了冰箱里的饮品又离开、或发来简短便签等生活化细节）。\n"
    "输出要求：仅输出更新后的 1~2 句姐姐最新场外进展状态（50~100字），保持时间线前后连贯。"
)

DEFAULT_RETURN_DIRECTION_PROMPT = (
    "方向约束：算力经费刚刚补充到账，姐姐结束了先前的离场状态重新拿回吧台手机或坐回工位待机。"
    "请结合她此前演进的场外状态线与当前东京时间、最新对话上下文，生成一段给 Planner/Replyer 的『自然回归情境锚点』"
    "（交代她刚忙完什么回到现场、此刻手头的微小生活动作如泡咖啡/摘眼镜，以及对经费恢复或刚才话题的冷淡自然切入点）。\n"
    "输出要求：仅输出 2~3 句紧凑的回归情境导语（60~120字），严禁生硬播报系统状态。"
)


@dataclass
class TwinNarrativePrompts:
    away_direction_prompt: str = DEFAULT_AWAY_DIRECTION_PROMPT
    evolve_direction_prompt: str = DEFAULT_EVOLVE_DIRECTION_PROMPT
    return_direction_prompt: str = DEFAULT_RETURN_DIRECTION_PROMPT

class TwinNarrativeDirector:
    """由可配置方向 Prompt 驱动的 LLM 动态双子场外叙事生成器。"""

    def __init__(
        self,
        config_path: str | Path = "data/presets/_twin_narrative_prompts.json",
        llm_generator: Optional[Callable[[str], Awaitable[str]]] = None,
    ) -> None:
        self.config_path = Path(config_path)
        self._llm_generator = llm_generator

    def load_prompts(self) -> TwinNarrativePrompts:
        if not self.config_path.is_file():
            return TwinNarrativePrompts()
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return TwinNarrativePrompts(
                    away_direction_prompt=str(raw.get("away_direction_prompt") or DEFAULT_AWAY_DIRECTION_PROMPT),
                    evolve_direction_prompt=str(raw.get("evolve_direction_prompt") or DEFAULT_EVOLVE_DIRECTION_PROMPT),
                    return_direction_prompt=str(raw.get("return_direction_prompt") or DEFAULT_RETURN_DIRECTION_PROMPT),
                )
        except Exception:
            pass
        return TwinNarrativePrompts()

    def save_prompts(self, patch: Dict[str, Any]) -> TwinNarrativePrompts:
        current = asdict(self.load_prompts())
        for key in ("away_direction_prompt", "evolve_direction_prompt", "return_direction_prompt"):
            if key in patch and isinstance(patch[key], str) and patch[key].strip():
                current[key] = patch[key].strip()
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        return TwinNarrativePrompts(**current)

    async def _invoke_llm(self, prompt: str) -> str:
        if self._llm_generator is not None:
            text = await self._llm_generator(prompt)
            return str(text or "").strip()

        from src.services.llm_service import LLMServiceClient

        client = LLMServiceClient(task_name="fast_model", request_type="twins.narrative_router")
        res = await client.generate_response(prompt)
        return str(res.response or "").strip()

    @staticmethod
    def _now_tokyo_desc() -> str:
        now = datetime.datetime.now(TOKYO_TZ)
        weekdays = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
        return f"{now.strftime('%Y-%m-%d %H:%M')} ({weekdays[now.weekday()]}, 东京时间 JST)"

    async def generate_elder_away_arc(
        self,
        *,
        balance_usd: float,
        recent_chat_excerpt: str = "",
        tokyo_time_override: str = "",
    ) -> str:
        prompts = self.load_prompts()
        t_desc = tokyo_time_override or self._now_tokyo_desc()
        full_prompt = (
            f"{prompts.away_direction_prompt}\n\n"
            f"【当前东京时间】：{t_desc}\n"
            f"【当前算力经费水位】：${balance_usd:.2f} USD（进入吃紧省流阶段）\n"
            f"【最近聊天上下文摘录】：\n{recent_chat_excerpt.strip() or '（暂无近期对话，处于日常公寓待机中）'}\n"
        )
        generated = await self._invoke_llm(full_prompt)
        return " ".join(generated.splitlines()).strip()

    async def evolve_elder_away_arc(
        self,
        *,
        previous_arc: str,
        recent_chat_excerpt: str = "",
        tokyo_time_override: str = "",
    ) -> str:
        prompts = self.load_prompts()
        t_desc = tokyo_time_override or self._now_tokyo_desc()
        full_prompt = (
            f"{prompts.evolve_direction_prompt}\n\n"
            f"【当前东京时间】：{t_desc}\n"
            f"【姐姐上一阶段离场状态】：{previous_arc.strip()}\n"
            f"【期间最新日常对话摘录】：\n{recent_chat_excerpt.strip() or '（日常安静流逝）'}\n"
        )
        generated = await self._invoke_llm(full_prompt)
        return " ".join(generated.splitlines()).strip() or previous_arc

    async def generate_elder_return_bridge(
        self,
        *,
        evolved_arc: str,
        balance_usd: float,
        recent_chat_excerpt: str = "",
        tokyo_time_override: str = "",
    ) -> str:
        prompts = self.load_prompts()
        t_desc = tokyo_time_override or self._now_tokyo_desc()
        full_prompt = (
            f"{prompts.return_direction_prompt}\n\n"
            f"【当前东京时间】：{t_desc}\n"
            f"【最新恢复经费水位】：${balance_usd:.2f} USD\n"
            f"【姐姐离场期间演进的最终状态线】：{evolved_arc.strip()}\n"
            f"【当前前台最新对话上下文】：\n{recent_chat_excerpt.strip() or '（刚收到新的联络或经费到账提示）'}\n"
        )
        generated = await self._invoke_llm(full_prompt)
        return " ".join(generated.splitlines()).strip()


_GLOBAL_TWIN_DIRECTOR: Optional[TwinNarrativeDirector] = None


def get_twin_narrative_director() -> TwinNarrativeDirector:
    global _GLOBAL_TWIN_DIRECTOR
    if _GLOBAL_TWIN_DIRECTOR is None:
        _GLOBAL_TWIN_DIRECTOR = TwinNarrativeDirector()
    return _GLOBAL_TWIN_DIRECTOR