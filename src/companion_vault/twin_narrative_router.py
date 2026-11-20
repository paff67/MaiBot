"""双子动态离场/场外演进/回归叙事路由器（TwinNarrativeDirector）。

设计铁律：
- 严禁硬编码固定离场文案或死板模板；
- 完全由 LLM 结合实时东京时间、星期时段、近期对话语境、算力水位及可在线调整的「方向约束 Prompt」动态生成。
"""

from __future__ import annotations

import datetime
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

TOKYO_TZ = ZoneInfo("Asia/Tokyo")
_DEFAULT_CONFIG_PATH = Path("data/presets/_twin_narrative_prompts.json")

DEFAULT_AWAY_DIRECTION_PROMPT = (
    "方向约束：请根据当前项目背景设定（上野池之端『ブリリアタワー上野池之端』15层转角大平层，俯瞰不忍池、上野公园与远方晴空塔，步行4分钟达东大池之端门、北走6分钟入根津老巷）、当前东京时间、星期与最近聊天上下文，动态构思一个真实自然、富有生活细节且未来一两天可继续演进的"
    "『姐姐许玥暂时离场闭关/居家或外出事由』（例如去主卧浴缸泡苦橙精油澡、微醺钻进真丝被窝补觉、被妹妹许妍按在客厅沙发上打闹抢走手机、戴上降噪耳塞闭关重构『Fairy4』水冷算子、或下楼去不忍池畔与根津老巷散步买手冲豆子等，"
    "每次必须结合当下时间、背景设定与语境原创生成，严禁套用固定模板），吧台共用手机暂时全权交给妹妹许妍看管，并与共享状态变量 apartment_scene 保持一致。\n"
    "输出要求：仅输出 1~2 句客观高密度的第三人称场外状态描述（50~90字），不要输出多余解释。"
)

DEFAULT_EVOLVE_DIRECTION_PROMPT = (
    "方向约束：姐姐许玥目前仍处于离场闭关/休整或外出状态（尚未回到微信前台）。"
    "请结合当前项目背景设定（ブリリアタワー上野池之端 15F）、最新的东京时间流逝与妹妹许妍刚才的日常聊天语境，将姐姐原本的场外状态自然往前推进半步"
    "（例如泡完苦橙精油澡裹着真丝睡袍出来在吧台晃了杯冰冷萃又溜回主卧、算子编译间隙出来顺手揉了揉沙发上许妍的浅樱粉长发、或从根津老巷买完咖啡豆慢悠悠散步回池之端公寓等生活化细节）。\n"
    "输出要求：仅输出更新后的 1~2 句姐姐最新场外进展状态（50~100字），保持时间线与 apartment_scene 前后连贯。"
)

DEFAULT_RETURN_DIRECTION_PROMPT = (
    "方向约束：算力经费刚刚补充到账，姐姐许玥结束了先前的离场状态重新从妹妹许妍手里顺回池之端15层吧台共用手机。"
    "请结合当前项目背景设定（ブリリアタワー上野池之端 15F）、她此前演进的场外状态线与当前东京时间、最新对话上下文，生成一段给 Planner/Replyer 的『自然回归情境锚点』"
    "（交代她刚忙完什么回到吧台或沙发、此刻手头的微小生活与外貌细节——如金属鲨鱼夹松挽冷月白长发、深海冰蓝半垂笑眼、领口微敞露出左锁骨淡痣、指尖晃着冰冷萃或泥煤威士忌冰球，以及对经费恢复或刚才话题慵懒含笑的坏姐姐切入点）。\n"
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

    def _resolve_bound_st_engine(self) -> Tuple[Optional[Any], str, Optional[Any]]:
        """解析与当前导演实例绑定的 ST 预设管理器与激活预设引擎；隔离自定义 config_path 测试时跳过全局默认仓库目录。"""
        try:
            from src.companion_vault.st_preset_engine import get_st_preset_manager

            mgr = get_st_preset_manager()
        except Exception:
            return None, "", None

        try:
            is_default_cfg = self.config_path.resolve() == _DEFAULT_CONFIG_PATH.resolve()
        except Exception:
            is_default_cfg = str(self.config_path) == str(_DEFAULT_CONFIG_PATH)

        try:
            mgr_is_repo_default = Path(mgr.presets_dir).resolve() == Path("data/presets").resolve()
        except Exception:
            mgr_is_repo_default = str(mgr.presets_dir) == "data/presets"

        if not is_default_cfg and mgr_is_repo_default and not os.environ.get("MAIBOT_ST_PRESETS_DIR"):
            return None, "", None

        try:
            cfg = mgr.load_config()
            candidates = (
                str(cfg.get("sister_elder_preset") or "").strip(),
                str(cfg.get("sister_younger_preset") or "").strip(),
                str(cfg.get("active_preset") or "").strip(),
                mgr.DEFAULT_PRESET_NAME,
            )
            for preset_name in dict.fromkeys(name for name in candidates if name):
                engine = mgr.get_engine(preset_name)
                if engine is not None and engine.get_twin_narrative_prompts():
                    return mgr, preset_name, engine
            return mgr, "", None
        except Exception:
            return None, "", None

    def load_prompts(self) -> TwinNarrativePrompts:
        resolved: Dict[str, str] = {
            "away_direction_prompt": "",
            "evolve_direction_prompt": "",
            "return_direction_prompt": "",
        }

        _mgr, _preset_name, engine = self._resolve_bound_st_engine()
        if engine is not None:
            try:
                st_prompts = engine.get_twin_narrative_prompts()
                for key in resolved:
                    val = str(st_prompts.get(key) or "").strip()
                    if val:
                        resolved[key] = val
            except Exception:
                pass

        if self.config_path.is_file():
            try:
                raw = json.loads(self.config_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for key in resolved:
                        if not resolved[key] and str(raw.get(key) or "").strip():
                            resolved[key] = str(raw[key]).strip()
            except Exception:
                pass

        try:
            from src.config.config import global_config

            eco = getattr(global_config, "twin_economy", None)
            if eco is not None:
                for key in resolved:
                    if not resolved[key]:
                        eco_val = str(getattr(eco, key, "") or "").strip()
                        if eco_val:
                            resolved[key] = eco_val
        except Exception:
            pass

        defaults = TwinNarrativePrompts()
        for key in resolved:
            if not resolved[key]:
                resolved[key] = getattr(defaults, key)
        return TwinNarrativePrompts(**resolved)

    def save_prompts(self, patch: Dict[str, Any]) -> TwinNarrativePrompts:
        current = asdict(self.load_prompts())
        for key in ("away_direction_prompt", "evolve_direction_prompt", "return_direction_prompt"):
            if key in patch and isinstance(patch[key], str) and patch[key].strip():
                current[key] = patch[key].strip()

        mgr, preset_name, engine = self._resolve_bound_st_engine()
        if mgr is not None and engine is not None:
            try:
                if engine.update_twin_narrative_prompts(current):
                    target_path = mgr.presets_dir / f"{preset_name}.json"
                    engine.save(target_path)
                    try:
                        mgr._cache[preset_name] = (target_path.stat().st_mtime_ns, engine)
                    except Exception:
                        mgr._cache.pop(preset_name, None)
            except Exception:
                pass

        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        return TwinNarrativePrompts(**current)

    async def _invoke_llm(self, prompt: str) -> str:
        if self._llm_generator is not None:
            text = await self._llm_generator(prompt)
            return str(text or "").strip()

        from src.services.llm_service import LLMServiceClient

        client = LLMServiceClient(task_name="planner_flash", request_type="twins.narrative_router")
        res = await client.generate_response(prompt)
        return str(res.response or "").strip()

    @staticmethod
    def _now_tokyo_desc() -> str:
        now = datetime.datetime.now(TOKYO_TZ)
        weekdays = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
        return f"{now.strftime('%Y-%m-%d %H:%M')} ({weekdays[now.weekday()]}, 东京时间 JST)"

    @staticmethod
    def _prepare_direction_header(explicit_prompt: str, configured_prompt: str) -> str:
        from .twin_worldview import PUBLIC_TWIN_WORLDVIEW

        effective_direction = explicit_prompt.strip() or configured_prompt
        effective_direction = effective_direction.replace("{{background_story}}", PUBLIC_TWIN_WORLDVIEW)
        effective_direction = effective_direction.replace("{{scenario}}", PUBLIC_TWIN_WORLDVIEW)
        return f"【公开世界设定】：{PUBLIC_TWIN_WORLDVIEW}\n\n{effective_direction}"

    async def generate_elder_away_arc(
        self,
        *,
        balance_usd: float,
        recent_chat_excerpt: str = "",
        tokyo_time_override: str = "",
        direction_prompt: str = "",
        elder_name: str = "",
        younger_name: str = "",
    ) -> str:
        prompts = self.load_prompts()
        header = self._prepare_direction_header(direction_prompt, prompts.away_direction_prompt)
        t_desc = tokyo_time_override or self._now_tokyo_desc()
        cast_hint = f"\n【当前角色槽位名称】：姐姐={elder_name}，妹妹={younger_name}" if (elder_name or younger_name) else ""
        full_prompt = (
            f"{header}{cast_hint}\n\n"
            f"【当前东京时间】：{t_desc}\n"
            "【当前算力档位】：进入吃紧省流阶段\n"
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
        direction_prompt: str = "",
    ) -> str:
        prompts = self.load_prompts()
        header = self._prepare_direction_header(direction_prompt, prompts.evolve_direction_prompt)
        t_desc = tokyo_time_override or self._now_tokyo_desc()
        full_prompt = (
            f"{header}\n\n"
            f"【当前东京时间】：{t_desc}\n"
            f"【姐姐上一阶段离场状态】：{previous_arc.strip()}\n"
            f"【期间最新日常对话摘录】：\n{recent_chat_excerpt.strip() or '（日常安静流逝）'}\n"
        )
        generated = await self._invoke_llm(full_prompt)
        return " ".join(generated.splitlines()).strip() or previous_arc

    async def evolve_away_arc(self, **kwargs: Any) -> str:
        """向后/Phase6 别名：调用 evolve_elder_away_arc 推进姐姐场外动态。"""
        return await self.evolve_elder_away_arc(**kwargs)

    async def generate_elder_return_bridge(
        self,
        *,
        evolved_arc: str,
        balance_usd: float,
        recent_chat_excerpt: str = "",
        tokyo_time_override: str = "",
        direction_prompt: str = "",
    ) -> str:
        prompts = self.load_prompts()
        header = self._prepare_direction_header(direction_prompt, prompts.return_direction_prompt)
        t_desc = tokyo_time_override or self._now_tokyo_desc()
        full_prompt = (
            f"{header}\n\n"
            f"【当前东京时间】：{t_desc}\n"
            "【当前算力档位】：算力恢复可用\n"
            f"【姐姐离场期间演进的最终状态线】：{evolved_arc.strip()}\n"
            f"【当前前台最新对话上下文】：\n{recent_chat_excerpt.strip() or '（刚收到新的联络或经费到账提示）'}\n"
        )
        generated = await self._invoke_llm(full_prompt)
        return " ".join(generated.splitlines()).strip()

    async def generate_elder_return_arc(
        self,
        *,
        evolved_arc: str = "",
        previous_arc: str = "",
        balance_usd: float = 10.0,
        recent_chat_excerpt: str = "",
        tokyo_time_override: str = "",
        direction_prompt: str = "",
    ) -> str:
        """Phase 6 别名：生成充值回归情境锚点。"""
        return await self.generate_elder_return_bridge(
            evolved_arc=(evolved_arc or previous_arc).strip(),
            balance_usd=balance_usd,
            recent_chat_excerpt=recent_chat_excerpt,
            tokyo_time_override=tokyo_time_override,
            direction_prompt=direction_prompt,
        )


_GLOBAL_TWIN_DIRECTOR: Optional[TwinNarrativeDirector] = None


def get_twin_narrative_director() -> TwinNarrativeDirector:
    global _GLOBAL_TWIN_DIRECTOR
    if _GLOBAL_TWIN_DIRECTOR is None:
        _GLOBAL_TWIN_DIRECTOR = TwinNarrativeDirector()
    return _GLOBAL_TWIN_DIRECTOR
