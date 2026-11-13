"""MaiBot 原生 WebUI 扩展插件：STPresetEngine 首页卡片 (@HomeCard) 与在线编辑/预览工作台 (@API)。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from maibot_sdk import API, HomeCard, MaiBotPlugin, PluginConfigBase
from pydantic import Field

from src.companion_vault.st_preset_engine import (
    STCompileContext,
    get_st_preset_manager,
)
from src.companion_vault.twin_narrative_router import get_twin_narrative_director
from src.llm_models.payload_content.context_item import (
    FunctionCallItem,
    FunctionCallOutputItem,
    get_item_text,
)


class PluginSectionConfig(PluginConfigBase):
    config_version: str = Field(default="1.0.0", description="配置版本")


class STPresetPluginConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig, description="插件元配置")
    presets_dir: str = Field(default="data/presets", description="酒馆预设 JSON 存储目录")
    auto_seed_preset_path: str = Field(default="", description="启动时自动导入的本地预设路径（可选）")


class STPresetEnginePlugin(MaiBotPlugin):
    """向 MaiBot 原生 WebUI 注册 `@HomeCard` 首页卡片、顶部概览卡片页与侧边栏在线预设编辑工作台。"""

    config_model = STPresetPluginConfig

    def __init__(self) -> None:
        super().__init__()
        self.set_plugin_config({})

    async def on_load(self) -> None:
        seed_path = str(self.config.auto_seed_preset_path or "").strip()
        if seed_path and Path(seed_path).is_file():
            await asyncio.to_thread(get_st_preset_manager().import_preset_file, seed_path)

    async def on_unload(self) -> None:
        return None

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        _ = version
        if scope == "self" and isinstance(config_data, dict):
            self.set_plugin_config(config_data)

    # ===== 原生 WebUI 首页卡片 (@HomeCard) =====

    @HomeCard(
        "st_preset_engine_card",
        title="ST 酒馆预设引擎 & 双子叙事",
        description="管理 SillyTavern 预设注入协议（SPreset 工具信封 / @D 深度插槽）与双子动态离场/回归方向约束。",
        content=[
            {
                "type": "markdown",
                "content": "**STPresetEngine** 支持无损编译社区高阶酒馆预设（如 `Meowssiah` 工具信封协议与标准 `@D` 深度插槽），并提供双子离场/演进/回归 LLM 动态叙事约束在线调优。",
            },
            {
                "type": "key_value",
                "entries": {
                    "支持协议": "SPreset 工具信封 / 标准 ST 深度插槽",
                    "双子动态路由": "LLM 实时语境生成（非硬编码）",
                    "隐私隔离": "物理四道防线 + scope_guard",
                },
            },
            {
                "type": "actions",
                "actions": [
                    {"label": "打开预设编辑工作台", "url": "/extensions/maibot-team.st-preset-engine/studio"},
                    {"label": "查看预设统计卡片页", "url": "/extensions/maibot-team.st-preset-engine/overview"},
                    {"label": "插件基础设置", "url": "/plugin-config?plugin=maibot-team.st-preset-engine"},
                ],
            },
        ],
        link_url="/extensions/maibot-team.st-preset-engine/studio",
        link_label="进入预设工作台",
        icon="settings",
        width="large",
        order=110,
    )
    async def st_preset_home_card(self) -> None:
        """向 MaiBot 原生 WebUI 首页注册 STPresetEngine 入口卡片。"""
        return None

    # ===== 原生 WebUI 扩展页 @API 绑定 =====

    @staticmethod
    def _inspect_engine(preset_name: str) -> Dict[str, Any]:
        mgr = get_st_preset_manager()
        engine = mgr.get_engine(preset_name)
        if engine is None:
            raise ValueError(f"未找到预设: {preset_name}")
        return {
            "preset_name": preset_name,
            "has_spreset_tool_envelope": engine.is_spreset_tool_envelope(),
            "entries": engine.inspect_entries(),
        }

    @API("webui_summary", description="为顶部工作区声明式卡片页提供状态统计与条目表格", version="1")
    async def webui_summary(self) -> Dict[str, Any]:
        mgr = get_st_preset_manager()
        cfg = mgr.load_config()
        presets = mgr.list_preset_names()
        active_name = str(cfg.get("active_preset") or (presets[0] if presets else ""))
        protocol_label = "未加载预设"
        entry_rows: List[Dict[str, Any]] = []
        if active_name:
            try:
                info = self._inspect_engine(active_name)
                protocol_label = "SPreset 工具信封" if info["has_spreset_tool_envelope"] else "标准 ST @D 深度插槽"
                for idx, e in enumerate(info["entries"], start=1):
                    grp = f"{e['role']}" + (f" · 互斥:{e['control_group']}" if e.get("control_group") else "")
                    entry_rows.append(
                        {
                            "order_index": idx,
                            "name": e["name"],
                            "state_text": "✅ 启用" if e["enabled"] else "⏸ 停用",
                            "group_text": grp,
                        }
                    )
            except Exception:
                protocol_label = "预设读取失败"
        elder_text = str(cfg.get("sister_elder_preset") or "") or "跟随默认预设"
        return {
            "enabled_label": "已启用接管" if cfg.get("enabled") else "待机（原生提示词）",
            "active_preset_label": active_name or "暂无预设",
            "protocol_label": protocol_label,
            "preset_count": len(presets),
            "binding_summary": (
                f"默认/妹妹预设：{active_name or '无'} ｜ 姐姐专属预设：{elder_text} ｜ 输出模式：{cfg.get('output_mode', 'im_clean')}"
            ),
            "entry_rows": entry_rows,
        }

    @API("webui_quick_toggle", description="快速切换全局 STPresetEngine 接管开关", version="1")
    async def webui_quick_toggle(self, enabled: bool = True) -> Dict[str, Any]:
        mgr = get_st_preset_manager()
        cfg = mgr.save_config({"enabled": bool(enabled)})
        return {"ok": True, "enabled": bool(cfg.get("enabled"))}

    @API("webui_status", description="查询引擎状态、可用预设列表与双子动态叙事配置", version="1")
    async def webui_status(self) -> Dict[str, Any]:
        mgr = get_st_preset_manager()
        director = get_twin_narrative_director()
        return {
            "engine_config": mgr.load_config(),
            "available_presets": mgr.list_preset_names(),
            "twin_narrative": {
                "config": asdict(director.load_prompts()),
            },
        }

    @API("webui_inspect_preset", description="读取指定酒馆预设的全部条目与元数据", version="1")
    async def webui_inspect_preset(self, preset_name: str) -> Dict[str, Any]:
        return self._inspect_engine(preset_name)

    @API("webui_save_config", description="保存 STPresetEngine 配置与双子动态叙事约束 Prompt", version="1")
    async def webui_save_config(
        self,
        enabled: bool,
        active_preset: str = "",
        sister_elder_preset: str = "",
        output_mode: str = "im_clean",
        away_direction_prompt: Optional[str] = None,
        evolve_direction_prompt: Optional[str] = None,
        return_direction_prompt: Optional[str] = None,
    ) -> Dict[str, Any]:
        mgr = get_st_preset_manager()
        cfg = mgr.save_config(
            {
                "enabled": bool(enabled),
                "active_preset": active_preset,
                "sister_elder_preset": sister_elder_preset,
                "output_mode": output_mode,
            }
        )
        director = get_twin_narrative_director()
        twin_updates: Dict[str, Any] = {}
        if away_direction_prompt is not None:
            twin_updates["away_direction_prompt"] = away_direction_prompt
        if evolve_direction_prompt is not None:
            twin_updates["evolve_direction_prompt"] = evolve_direction_prompt
        if return_direction_prompt is not None:
            twin_updates["return_direction_prompt"] = return_direction_prompt
        prompts = director.save_prompts(twin_updates) if twin_updates else director.load_prompts()
        return {"ok": True, "engine_config": cfg, "twin_config": asdict(prompts)}

    @API("webui_update_entry", description="更新单条预设条目的开关（含 radio 互斥）、内容与深度参数", version="1")
    async def webui_update_entry(
        self,
        preset_name: str,
        identifier: str,
        enabled: Optional[bool] = None,
        name: Optional[str] = None,
        role: Optional[str] = None,
        injection_position: Optional[int] = None,
        injection_depth: Optional[int] = None,
        scope_guard: Optional[str] = None,
        content: Optional[str] = None,
    ) -> Dict[str, Any]:
        mgr = get_st_preset_manager()
        engine = mgr.get_engine(preset_name)
        if engine is None:
            raise ValueError(f"未找到预设: {preset_name}")
        target_path = mgr.presets_dir / f"{preset_name}.json"
        engine.update_entry(
            identifier,
            enabled=enabled,
            name=name,
            role=role,
            injection_position=injection_position,
            injection_depth=injection_depth,
            scope_guard=scope_guard,
            content=content,
        )
        await asyncio.to_thread(engine.save, target_path)
        return {"ok": True, "preset": self._inspect_engine(preset_name)}

    @API("webui_reorder_entries", description="调整预设条目注入顺序", version="1")
    async def webui_reorder_entries(self, preset_name: str, ordered_identifiers: List[str]) -> Dict[str, Any]:
        mgr = get_st_preset_manager()
        engine = mgr.get_engine(preset_name)
        if engine is None:
            raise ValueError(f"未找到预设: {preset_name}")
        engine.reorder_entries(ordered_identifiers)
        await asyncio.to_thread(engine.save, mgr.presets_dir / f"{preset_name}.json")
        return {"ok": True, "preset": self._inspect_engine(preset_name)}

    @API("webui_import_preset_raw", description="直接导入 64KB 以内的 JSON 预设文本", version="1")
    async def webui_import_preset_raw(self, name: str, raw_json_str: str) -> Dict[str, Any]:
        mgr = get_st_preset_manager()
        raw_obj = json.loads(raw_json_str)
        saved_name = await asyncio.to_thread(mgr.import_preset_json, name, raw_obj)
        return {"ok": True, "preset_name": saved_name}

    @API("webui_import_local_path", description="从服务器本地文件路径导入任意大小的酒馆预设 JSON", version="1")
    async def webui_import_local_path(self, file_path: str, alias_name: str = "") -> Dict[str, Any]:
        src = Path(file_path.strip().strip('"'))
        if not src.is_file() or src.suffix.lower() != ".json":
            raise ValueError(f"未找到有效的本地 JSON 预设文件: {src}")
        mgr = get_st_preset_manager()
        saved_name = await asyncio.to_thread(mgr.import_preset_file, src, alias_name.strip())
        return {"ok": True, "preset_name": saved_name}

    @API("webui_preview_compile", description="实时 Dry-Run 编译预览预设生成的 ContextItem 与工具信封结构", version="1")
    async def webui_preview_compile(self, preset_name: str, last_user_message: str) -> Dict[str, Any]:
        mgr = get_st_preset_manager()
        engine = mgr.get_engine(preset_name)
        if engine is None:
            raise ValueError(f"未找到预设: {preset_name}")
        ctx = STCompileContext(
            char_name="姐姐",
            user_name="你",
            persona_description="温柔清醒、与你长久相伴的双子姐姐。",
            last_user_message=last_user_message,
            is_owner_private=True,
        )
        compiled = engine.compile_to_context_items(ctx)
        serialized: List[Dict[str, Any]] = []
        for idx, item in enumerate(compiled.context_items):
            if isinstance(item, FunctionCallItem):
                serialized.append(
                    {
                        "index": idx,
                        "kind": "function_call",
                        "name": item.tool_call.func_name,
                        "arguments": json.loads(item.tool_call.args_json.decode("utf-8")),
                    }
                )
            elif isinstance(item, FunctionCallOutputItem):
                serialized.append(
                    {
                        "index": idx,
                        "kind": "function_call_output",
                        "name": item.tool_name,
                        "output": item.output[:1200],
                    }
                )
            else:
                serialized.append(
                    {"index": idx, "kind": "message", "role": item.role.value, "text": get_item_text(item)[:1200]}
                )
        return {
            "preset_name": preset_name,
            "protocol": compiled.protocol,
            "item_count": len(compiled.context_items),
            "tool_count": len(compiled.tool_definitions or []),
            "extra_params": compiled.extra_params,
            "items": serialized,
        }


def create_plugin() -> STPresetEnginePlugin:
    return STPresetEnginePlugin()
