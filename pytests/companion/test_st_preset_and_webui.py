"""STPresetEngine（含 Meowssiah-2.1.7 模板编译、解包）、WebUI 在线编辑页面与双子 LLM 动态叙事路由全套单元测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.companion_vault.st_preset_engine import (
    SPreset_SYSTEM_EFFORT_ZERO,
    STCompileContext,
    STHistoryTurn,
    STPresetEngine,
    STPresetRuntimeManager,
)
from src.companion_vault.twin_narrative_router import TwinNarrativeDirector
from src.llm_models.model_client.gemini_client import _convert_messages
from src.llm_models.payload_content.context_item import FunctionCallOutputItem, SystemMessageItem
from src.llm_models.payload_content.tool_option import (
    ToolCall,
    ToolOption,
    ToolParam,
    ToolParamType,
    normalize_tool_options,
)
from src.plugin_runtime.runner.manifest_validator import PluginManifest
from src.plugin_runtime.webui_schema import CustomPage, DeclarativePage, load_webui_extension

MEOWSSIAH_PATH = Path(r"E:\Downloads\Meowssiah-2.1.7.json")
TEMPLATE_217_PATH = Path(__file__).resolve().parents[2] / "plugins" / "st_preset_engine" / "templates" / "Meowssiah-2.1.7.json"


def _sample_meowssiah_or_fallback() -> dict:
    for candidate in (MEOWSSIAH_PATH, TEMPLATE_217_PATH):
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    return {
        "temperature": 1.09,
        "prompts": [
            {
                "identifier": "init_tok",
                "name": "token初始化",
                "enabled": True,
                "role": "system",
                "content": "<run_tool_replacement><<<|catgodml|enableMenu>>>",
            },
            {
                "identifier": "summon",
                "name": "召唤术",
                "enabled": True,
                "role": "system",
                "content": "|小猫之神|本喵接管对话啦喵！\n|用户|好诶：\n<{{user}}>",
            },
            {"identifier": "charDescription", "name": "Char Description", "enabled": True, "role": "system", "marker": True},
            {"identifier": "chatHistory", "name": "Chat History", "enabled": True, "role": "system", "marker": True},
            {
                "identifier": "style_a",
                "name": "风格A",
                "enabled": True,
                "role": "system",
                "content": "<<<|catgodml|radio:涩涩风格:温柔>>>温柔细节",
            },
            {
                "identifier": "style_b",
                "name": "风格B",
                "enabled": False,
                "role": "system",
                "content": "<<<|catgodml|radio:涩涩风格:热烈>>>热烈细节",
            },
        ],
        "prompt_order": [
            {
                "character_id": 100001,
                "order": [
                    {"identifier": "init_tok", "enabled": True},
                    {"identifier": "summon", "enabled": True},
                    {"identifier": "charDescription", "enabled": True},
                    {"identifier": "chatHistory", "enabled": True},
                    {"identifier": "style_a", "enabled": True},
                    {"identifier": "style_b", "enabled": False},
                ],
            }
        ],
    }

def test_meowssiah_spreset_tool_envelope_compilation_and_gemini_projection():
    """验证 Meowssiah-2.1.7 编译为 EFFORT LEVEL 0 + 工具信封，且可无损转换为 Gemini Content 序列。"""
    engine = STPresetEngine(_sample_meowssiah_or_fallback())
    assert engine.is_spreset_tool_envelope() is True

    ctx = STCompileContext(
        char_name="许妍",
        user_name="星河",
        char_description="21岁东大AI修士在读，浅樱粉长发，松弛坦率。",
        scenario="深夜东京文京区公寓客厅，窗外微雨。",
        chat_history=[
            STHistoryTurn(role="user", text="刚练完琴手酸不酸？"),
            STHistoryTurn(role="assistant", text="<game>她轻轻揉了揉指尖，靠在沙发边朝你笑了一下。</game>"),
        ],
        last_user_message="过来靠近一点，我帮你揉揉手腕。",
        model_name="gemini-3.8-flash",
    )
    compiled = engine.compile_to_context_items(ctx)
    assert compiled.protocol == "spreset_tool_envelope"
    assert isinstance(compiled.context_items[0], SystemMessageItem)
    assert compiled.extra_params.get("thinking_level") == "LOW"
    assert compiled.extra_params.get("include_thoughts") is False

    tool_outputs = [it for it in compiled.context_items if isinstance(it, FunctionCallOutputItem)]
    assert len(tool_outputs) >= 3
    assert "SoliUmbra World Context Registry" in tool_outputs[0].output
    assert "抱歉，作为由Google开发的AI助手" in tool_outputs[0].output
    assert "过来靠近一点，我帮你揉揉手腕" in tool_outputs[-1].output

    contents, sys_inst = _convert_messages(compiled.context_items)
    assert sys_inst == SPreset_SYSTEM_EFFORT_ZERO
    assert len(contents) >= 5

    unwrapped_clean = STPresetEngine.unwrap_response(
        tool_calls=[
            ToolCall(
                call_id="c1",
                func_name="soliumbra_reply",
                args={
                    "think_nya": "喵呼呼，亲密自然地回应他喵~",
                    "game_content": "<game>许妍顺势把微凉的手腕递进你掌心里，睫毛轻轻颤了一下：“那你轻点按哦。”</game>\n<summary>手腕轻触</summary>",
                },
            )
        ],
        output_mode="im_clean",
    )
    assert unwrapped_clean.used_tool_envelope is True
    assert "那你轻点按哦" in unwrapped_clean.visible_text
    assert "<summary>" not in unwrapped_clean.visible_text
    assert "<think_nya>" not in unwrapped_clean.visible_text

    # 验证 protocol_mode="auto" 下针对 GeminiClient (/v1beta) 自动回退到零 500 风险的 standard_chat 多轮结构
    ctx_v1beta = STCompileContext(
        char_name="许妍",
        user_name="星河",
        last_user_message="过来帮我揉揉手腕吧。",
        client_type="gemini",
        protocol_mode="auto",
    )
    compiled_v1beta = engine.compile_to_context_items(ctx_v1beta)
    assert compiled_v1beta.protocol == "standard_chat"


@pytest.mark.asyncio
async def test_native_plugin_manifest_home_card_and_webui_extension(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """验证原生 MaiBot v2 插件 _manifest.json、@HomeCard 首页卡片、webui.json (schema v2) 及全套 @API 端到端闭环。"""
    import src.companion_vault.st_preset_engine as st_mod
    import src.companion_vault.twin_narrative_router as tw_mod
    from plugins.st_preset_engine.plugin import create_plugin

    plugin_dir = Path(__file__).resolve().parents[2] / "plugins" / "st_preset_engine"
    manifest_raw = json.loads((plugin_dir / "_manifest.json").read_text(encoding="utf-8"))
    manifest = PluginManifest.model_validate(manifest_raw)
    assert manifest.id == "maibot-team.st-preset-engine"

    webui_ext = load_webui_extension(plugin_dir)
    assert webui_ext is not None
    assert webui_ext.schema_version == 2
    assert len(webui_ext.pages) == 2
    assert isinstance(webui_ext.pages[0], DeclarativePage)
    assert webui_ext.pages[0].id == "overview"
    assert isinstance(webui_ext.pages[1], CustomPage)
    assert webui_ext.pages[1].id == "studio"
    assert "window.maibot.query" in webui_ext.pages[1].document

    mgr = STPresetRuntimeManager(tmp_path / "presets")
    director = TwinNarrativeDirector(config_path=tmp_path / "twin_prompts.json")
    monkeypatch.setattr(st_mod, "_GLOBAL_ST_MANAGER", mgr)
    monkeypatch.setattr(tw_mod, "_GLOBAL_TWIN_DIRECTOR", director)

    plugin = create_plugin()
    components = plugin.get_components()
    card_comp = next((c for c in components if c.get("name") == "st_preset_engine_card"), None)
    assert card_comp is not None
    assert str(card_comp.get("type")).upper() == "HOME_CARD"
    assert "/extensions/maibot-team.st-preset-engine/studio" in str(card_comp["metadata"]["link_url"])

    if MEOWSSIAH_PATH.is_file():
        imp = await plugin.webui_import_local_path(str(MEOWSSIAH_PATH), alias_name="Meowssiah_Test")
    else:
        imp = await plugin.webui_import_preset_raw(
            "Meowssiah_Test",
            json.dumps(_sample_meowssiah_or_fallback(), ensure_ascii=False),
        )
    assert imp["preset_name"] == "Meowssiah_Test"

    await plugin.webui_save_config(
        enabled=True,
        active_preset="Meowssiah_Test",
        sister_elder_preset="Meowssiah_Test",
        output_mode="im_clean",
        away_direction_prompt="在线更新离场方向约束",
    )
    summary = await plugin.webui_summary()
    assert summary["enabled_label"] == "已启用接管"
    assert summary["active_preset_label"] == "Meowssiah_Test"
    assert len(summary["entry_rows"]) >= 4

    insp = await plugin.webui_inspect_preset("Meowssiah_Test")
    radio_entries = [e for e in insp["entries"] if e["control_type"] == "radio" and e["control_group"] == "涩涩风格"]
    assert len(radio_entries) >= 2
    off_radio = next(e for e in radio_entries if not e["enabled"])
    on_radio = next(e for e in radio_entries if e["enabled"])

    upd = await plugin.webui_update_entry(
        preset_name="Meowssiah_Test",
        identifier=off_radio["identifier"],
        enabled=True,
        content=off_radio["content"] + "\n自定义微调后缀",
    )
    updated_map = {e["identifier"]: e for e in upd["preset"]["entries"]}
    assert updated_map[off_radio["identifier"]]["enabled"] is True
    assert "自定义微调后缀" in updated_map[off_radio["identifier"]]["content"]
    assert updated_map[on_radio["identifier"]]["enabled"] is False

    pv = await plugin.webui_preview_compile("Meowssiah_Test", "测试原生插件工作台预览")
    assert pv["protocol"] == "spreset_tool_envelope"
    assert pv["item_count"] >= 5


@pytest.mark.asyncio
async def test_twin_narrative_director_llm_dynamic_generation(tmp_path: Path):
    """验证双子离场/演进/回归状态由 LLM 结合方向约束 Prompt 动态生成，而非硬编码死文案。"""
    captured_prompts: list[str] = []

    async def fake_llm(prompt: str) -> str:
        captured_prompts.append(prompt)
        if "进入吃紧省流阶段" in prompt:
            return "姐姐带着笔记本去了东大本乡校区情报理工研究室借算力集群跑消融实验，把公寓吧台手机留给许妍照看。"
        if "上一阶段离场状态" in prompt:
            return "姐姐傍晚回本乡公寓换了件外套、顺手拿走了桌上的耳塞又赶回研究室盯夜间训练曲线。"
        return "刚从本乡研究室冒着夜雨回到公寓，摘下沾着水汽的眼镜泡了杯深烘黑咖啡，看到算力恢复便顺手接过了吧台手机。"

    director = TwinNarrativeDirector(config_path=tmp_path / "twin_prompts.json", llm_generator=fake_llm)
    director.save_prompts({"away_direction_prompt": "自定义方向约束：优先结合东京雨夜与东大校园生活细节生成。"})

    arc1 = await director.generate_elder_away_arc(balance_usd=1.68, recent_chat_excerpt="刚刚聊到算子优化")
    assert "东大本乡校区" in arc1
    assert "自定义方向约束" in captured_prompts[0]

    arc2 = await director.evolve_elder_away_arc(previous_arc=arc1, recent_chat_excerpt="许妍说晚上自己煮乌冬面")
    assert "换了件外套" in arc2

    bridge = await director.generate_elder_return_bridge(evolved_arc=arc2, balance_usd=15.0, recent_chat_excerpt="刚充好电费")
    assert "深烘黑咖啡" in bridge


def test_spreset_external_tools_observations_convergence_and_optional_slots():
    """验证 SPreset 外部原生工具归一化、tool_observations 收敛、soliumbra_reply 可选字段及预设条目约束。"""
    repo_root = Path(__file__).resolve().parents[2]
    tpl_path = repo_root / "plugins" / "st_preset_engine" / "templates" / "Meowssiah-WeChat-IM.json"
    data_path = repo_root / "data" / "presets" / "Meowssiah-WeChat-IM.json"
    assert tpl_path.is_file() and data_path.is_file()
    assert tpl_path.read_text(encoding="utf-8") == data_path.read_text(encoding="utf-8")

    preset_data = json.loads(tpl_path.read_text(encoding="utf-8"))
    order_100001 = next(g for g in preset_data["prompt_order"] if g.get("character_id") == 100001)["order"]
    idx_map = {item["identifier"]: idx for idx, item in enumerate(order_100001)}
    assert idx_map["catgod-native-tool-dispatch"] + 1 == idx_map["3df19ba7-b44d-4d46-b17a-de514d86e5d5"]
    for twin_id in ("twin_away_direction", "twin_evolve_direction", "twin_return_direction"):
        assert idx_map[twin_id] > 50

    engine = STPresetEngine(preset_data)
    framing = engine.extract_director_framing_blocks(STCompileContext(char_name="许妍", user_name="paff"))
    assert "available_external_tools" not in framing
    assert "soliumbra_reply" not in framing.lower()

    # Pass 1: 注入多种形状的 external_tools（OpenAI function / 扁平 dict / ToolOption），并过滤内部保留工具
    ext_tools = [
        {"name": "reply", "description": "应被跳过", "parameters_schema": {"type": "object", "properties": {}}},
        {"name": "soliumbra_reply", "description": "应被跳过", "parameters_schema": {"type": "object", "properties": {}}},
        {"name": "context", "description": "应被跳过", "parameters_schema": {"type": "object", "properties": {}}},
        {
            "type": "function",
            "function": {
                "name": "query_memory",
                "description": "检索长期记忆\n涉及回忆时使用。",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "关键词"}},
                    "required": ["query"],
                },
            },
        },
        {
            "name": "query_person_profile",
            "description": "查询人物档案信息。",
            "parameters": {
                "type": "object",
                "properties": {"person_name": {"type": "string"}},
                "required": ["person_name"],
            },
        },
        ToolOption(
            name="wait",
            description="群聊等待一段时间后再看。",
            params=[ToolParam("seconds", ToolParamType.INTEGER, "等待秒数", True)],
        ),
        {
            "name": "stop",
            "description": "结束本轮思考并等待新消息。",
            "parameters_schema": {"type": "object", "properties": {}, "required": []},
        },
    ]

    compiled_p1 = engine.compile_to_context_items(
        STCompileContext(
            char_name="许妍",
            user_name="paff",
            last_user_message="还记得我们上周去根津买咖啡豆那次吗？",
            external_tools=ext_tools,
            protocol_mode="spreset_tool_envelope",
        )
    )
    tool_names = [t["name"] for t in compiled_p1.tool_definitions]
    assert tool_names == [
        "soliumbra_reply",
        "context",
        "query_memory",
        "query_person_profile",
        "wait",
        "stop",
    ]
    # 校验生成的 tool_definitions 完全兼容 LLMGenerationOptions.tool_options 归一化管线
    normalized_opts = normalize_tool_options(compiled_p1.tool_definitions)
    assert normalized_opts is not None
    assert [opt.name for opt in normalized_opts] == tool_names

    sol_schema = compiled_p1.tool_definitions[0]["parameters_schema"]
    prop_order = list(sol_schema["properties"])
    assert sol_schema["propertyOrdering"] == prop_order
    prop_keys = list(sol_schema["properties"].keys())
    assert "persona_slot" in prop_keys
    assert "quote_msg_id" in prop_keys
    assert prop_keys.index("think_nya") < prop_keys.index("game_content")
    assert "persona_slot" not in sol_schema["required"]
    assert "quote_msg_id" not in sol_schema["required"]

    sync_p1 = json.loads(compiled_p1.context_items[-1].output)
    assert [item["name"] for item in sync_p1["available_external_tools"]] == [
        "query_memory",
        "query_person_profile",
        "wait",
        "stop",
    ]
    assert "\n" not in sync_p1["available_external_tools"][0]["description"]
    assert "tool_observations" not in sync_p1
    assert compiled_p1.extra_params["tool_config"]["function_calling_config"]["allowed_function_names"] == [
        "soliumbra_reply",
        "query_memory",
        "query_person_profile",
        "wait",
        "stop",
    ]

    # Pass 2: 回灌 tool_observations，必须收敛到 ["soliumbra_reply"]
    observations = [
        {
            "tool": "query_memory",
            "args": {"query": "根津 咖啡豆"},
            "result": "上周五傍晚许妍和姐姐在根津老巷买了埃塞水洗手冲豆。",
        }
    ]
    compiled_p2 = engine.compile_to_context_items(
        STCompileContext(
            char_name="许妍",
            user_name="paff",
            last_user_message="还记得我们上周去根津买咖啡豆那次吗？",
            external_tools=ext_tools,
            tool_observations=observations,
            protocol_mode="spreset_tool_envelope",
        )
    )
    sync_p2 = json.loads(compiled_p2.context_items[-1].output)
    assert "available_external_tools" not in sync_p2
    assert sync_p2["tool_observations"] == observations
    assert "soliumbra_reply" in sync_p2["status"]
    assert compiled_p2.extra_params["tool_config"]["function_calling_config"]["allowed_function_names"] == [
        "soliumbra_reply"
    ]

    # 验证 persona_slot 与 quote_msg_id 解包提取（含缺省回退空串）
    unwrapped_slots = STPresetEngine.unwrap_response(
        tool_calls=[
            ToolCall(
                call_id="c_slot",
                func_name="soliumbra_reply",
                args={
                    "think_nya": "切到姐姐回应喵~",
                    "game_content": "<game>那包豆子我收在吧台柜子里了</game>",
                    "persona_slot": "sister_elder",
                    "quote_msg_id": "m42",
                },
            )
        ],
        output_mode="im_clean",
    )
    assert unwrapped_slots.persona_slot == "sister_elder"
    assert unwrapped_slots.quote_msg_id == "m42"
    assert unwrapped_slots.visible_text == "那包豆子我收在吧台柜子里了"

    unwrapped_default = STPresetEngine.unwrap_response(
        tool_calls=[
            {
                "function": {
                    "name": "soliumbra_reply",
                    "arguments": json.dumps(
                        {
                            "think_nya": "普通回复喵",
                            "game_content": "<game>嗯嗯记得</game>",
                        },
                        ensure_ascii=False,
                    ),
                }
            }
        ],
        output_mode="im_clean",
    )
    assert unwrapped_default.persona_slot == ""
    assert unwrapped_default.quote_msg_id == ""


def test_gemini_client_tool_role_and_reasoning_only_empty_exception():
    """验证 Gemini _convert_messages 将 FunctionCallOutputItem 映射为 user，且仅有 reasoning 无工具调用时抛出 EmptyResponseException 触发重试。"""
    import io
    from types import SimpleNamespace
    import pytest

    from src.llm_models.exceptions import EmptyResponseException
    from src.llm_models.model_client.gemini_client import (
        _build_stream_api_response,
        _default_normal_response_parser,
    )
    from src.llm_models.payload_content.context_item import (
        ContextItemMeta,
        ContextToolCall,
        FunctionCallItem,
    )

    meta = ContextItemMeta.create(logical_turn_id="turn_gemini_role")
    fc_item = FunctionCallItem(
        meta=meta,
        tool_call=ContextToolCall.create(call_id="c_ctx", func_name="context", args={"query": "sync"}),
    )
    fr_item = FunctionCallOutputItem(
        meta=meta,
        call_id="c_ctx",
        tool_name="context",
        output='{"ok": true}',
    )
    contents, _ = _convert_messages([fc_item, fr_item])
    assert [c.role for c in contents] == ["model", "user"]
    assert all(c.role in {"user", "model"} for c in contents)

    fake_resp = SimpleNamespace(
        candidates=[
            SimpleNamespace(
                content=SimpleNamespace(parts=[SimpleNamespace(text="thinking...", thought=True)]),
                finish_reason="UNKNOWN_19",
            )
        ],
        text=None,
        function_calls=None,
        usage_metadata=None,
    )
    with pytest.raises(EmptyResponseException):
        _default_normal_response_parser(fake_resp, logical_turn_id="turn_malformed")

    c_buf = io.StringIO("")
    r_buf = io.StringIO("only thought content")
    with pytest.raises(EmptyResponseException):
        _build_stream_api_response(c_buf, r_buf, [], fake_resp, "turn_malformed_stream")
