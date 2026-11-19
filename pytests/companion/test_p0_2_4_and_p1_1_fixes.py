"""P0-2（打字上限+edge断连重试）、P0-4（微信媒体形状+就绪门控）、P1-1（整批双子切槽+零侵入MVU）回归测试。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from src.chat.debounce import MessageDebouncer
from src.chat.debounce.message_adapter import get_message_shape
from src.chat.utils import utils as chat_utils
from src.common.data_models.message_component_data_model import MessageSequence, TextComponent
from src.companion_vault.compute_ledger import ComputeLedger
from src.companion_vault.mvu_engine import MVURuntimeEngine
from src.companion_vault.twin_runtime_router import resolve_turn_route
from src.maisaka import runtime as runtime_module
from src.maisaka.runtime import MaisakaHeartFlowChatting
from src.maisaka.turn_trigger import scheduler as scheduler_module
from src.maisaka.turn_trigger.scheduler import MessageTurnScheduler
from src.platform_io.wechat.edge_client import WeChatEdgeClient
from src.platform_io.wechat.projection import project_message


# ---------------------------------------------------------------------------
# P0-2: 模拟打字上限封顶 + WeChatEdgeClient RemoteProtocolError 单次幂等重试
# ---------------------------------------------------------------------------


def test_calculate_typing_time_caps_long_bubbles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        chat_utils,
        "global_config",
        SimpleNamespace(response_post_process=SimpleNamespace(typing_speed=1.0)),
    )
    # 150 个汉字原本会算出 45s，封顶后应限制在默认 3.5s
    long_text = "土" * 150
    assert chat_utils.calculate_typing_time(long_text) == pytest.approx(3.5)
    # 短气泡保持自然线性时间（4 个汉字 = 1.2s）
    assert chat_utils.calculate_typing_time("你好呀吗") == pytest.approx(1.2)
    # 单汉字保持 3 倍 + 0.3s = 1.2s
    assert chat_utils.calculate_typing_time("好") == pytest.approx(1.2)
    # 自定义上限生效
    assert chat_utils.calculate_typing_time(long_text, max_typing_time=2.0) == pytest.approx(2.0)
    # typing_speed <= 0 返回 0
    monkeypatch.setattr(
        chat_utils,
        "global_config",
        SimpleNamespace(response_post_process=SimpleNamespace(typing_speed=0)),
    )
    assert chat_utils.calculate_typing_time(long_text) == 0


@pytest.mark.asyncio
async def test_edge_client_retries_once_on_remote_protocol_error_with_same_request_id() -> None:
    calls: list[dict] = []

    class FlakyClient:
        async def post(self, url: str, json=None):
            calls.append({"url": url, "json": dict(json or {})})
            if len(calls) == 1:
                raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
            return httpx.Response(
                200,
                json={"ok": True, "sent": True, "status": "confirmed", "msg_id": "143409"},
                request=httpx.Request("POST", url),
            )

    edge = WeChatEdgeClient("http://edge.local:8765", "acc1", client=FlakyClient(), use_hook_send=True)
    res = await edge.send_text("45009933541@chatroom", "气泡二测试", "internal-msg-2")
    assert res["status"] == "confirmed"
    assert res["msg_id"] == "143409"
    assert len(calls) == 2
    assert calls[0]["json"]["request_id"] == calls[1]["json"]["request_id"]


@pytest.mark.asyncio
async def test_edge_client_disables_stale_keepalive_pool() -> None:
    edge = WeChatEdgeClient("http://127.0.0.1:18765", "acc1", token="tok")
    try:
        pool = getattr(edge.client._transport, "_pool", None)
        assert pool is not None
        assert getattr(pool, "_max_keepalive_connections", None) == 0
    finally:
        await edge.close()


# ---------------------------------------------------------------------------
# P0-4: 微信媒体形状识别（裸图 4s 防抖）+ 媒体原件就绪门控
# ---------------------------------------------------------------------------


def _make_wechat_msg(
    text: str,
    *,
    msg_id: str = "wechat:src:acc1:143404",
    wechat_msg_id: str = "143404",
    wechat_msg_type: int | None = 3,
    media_state: str = "resolve_from_vault",
    chat_id: str = "45009933541@chatroom",
    account_id: str = "acc1",
) -> SimpleNamespace:
    add_cfg = {
        "wechat_msg_id": wechat_msg_id,
        "wechat_media_state": media_state,
    }
    if wechat_msg_type is not None:
        add_cfg["wechat_msg_type"] = wechat_msg_type
    return SimpleNamespace(
        platform="wechat",
        message_id=msg_id,
        account_id=account_id,
        processed_plain_text=text,
        raw_message=MessageSequence(components=[TextComponent(text=text)]),
        message_info=SimpleNamespace(
            user_info=SimpleNamespace(user_id="wxid_paff"),
            group_info=SimpleNamespace(group_id=chat_id),
            additional_config=add_cfg,
        ),
    )


def test_wechat_projected_image_and_emoji_trigger_proper_debounce_shape() -> None:
    row = {
        "msgId": "143404",
        "talker": "45009933541@chatroom",
        "type": 3,
        "createTime": 1795085869000,
    }
    projected = project_message(
        {
            "source": "https://edge.local",
            "account": "a" * 32,
            "msg_id": "143404",
            "chat_id": "45009933541@chatroom",
            "direction": "incoming",
            "sender_id": "wxid_paff",
            "body": "",
            "revision": "rev1",
            "row_json": json.dumps(row),
        }
    )
    assert projected["message_info"]["additional_config"]["wechat_msg_type"] == 3
    assert projected["message_info"]["additional_config"]["wechat_media_state"] == "resolve_from_vault"

    img_msg = _make_wechat_msg("[图片]", wechat_msg_type=3)
    assert get_message_shape(img_msg) == (1, 0)

    # 验证 MessageDebouncer 确实走裸图至少 4 秒规则，而非 2 秒
    debouncer = MessageDebouncer()
    reason = debouncer.observe(
        author="wxid_paff",
        is_bare_image=True,
        is_pure_emoji=False,
        at_bot=False,
        received_at=1000.0,
        wall_time=1000.0,
        now=100.0,
        mode="natural",
    )
    assert "裸图开批至少 4 秒" in reason
    assert debouncer.deadline == pytest.approx(104.0)

    emoji_msg = _make_wechat_msg("[表情包]", wechat_msg_type=47)
    assert get_message_shape(emoji_msg) == (2, 0)

    # 微信引用回复（type=49 + 普通文字）不应被误判为裸媒体
    quote_msg = _make_wechat_msg("这道题怎么看", wechat_msg_type=49)
    assert get_message_shape(quote_msg) == (0, 6)


def test_wechat_media_hydration_gate_defers_until_ready_or_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "truth.db"
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE message_media_jobs ("
        "source TEXT, account TEXT, chat_id TEXT, msg_id TEXT, ref_index INTEGER, state TEXT, updated_at REAL)"
    )
    con.execute(
        "INSERT INTO message_media_jobs VALUES ('s1', 'acc1', '45009933541@chatroom', '143404', 0, 'running', 1000.0)"
    )
    con.commit()
    con.close()

    monkeypatch.setenv("MAIBOT_TRUTH_DB_PATH", str(db_path))
    monkeypatch.setenv("MAIBOT_MEDIA_HYDRATION_MAX_WAIT_S", "10.0")
    monkeypatch.delenv("MAIBOT_DISABLE_MEDIA_WAIT", raising=False)

    now_box = [100.0]
    monkeypatch.setattr(runtime_module.time, "monotonic", lambda: now_box[0])
    monkeypatch.setattr(
        scheduler_module,
        "global_config",
        SimpleNamespace(chat=SimpleNamespace(reply_timing=SimpleNamespace(planner_message_steering=False))),
    )

    img_msg = _make_wechat_msg("[图片]", wechat_msg_id="143404", wechat_msg_type=3)
    rt = object.__new__(MaisakaHeartFlowChatting)
    rt.message_cache = [img_msg]
    rt._last_processed_index = 0
    rt._media_hydration_first_seen = {}
    rt._agent_state = "running"
    rt._STATE_WAIT = "wait"
    rt._message_turn_scheduled = False
    rt.chat_stream = SimpleNamespace(is_group_session=True)
    rt._get_pending_message_count = lambda: 1
    rt._get_effective_reply_frequency = lambda: 1.0
    rt._is_reply_frequency_silent = lambda: False
    rt._has_forced_turn_trigger = lambda: False
    rt._idle_backoff = SimpleNamespace(get_delay_seconds=lambda n: 0.0)
    rt._get_message_quiet_period_remaining_seconds = lambda: 0.0
    rt._defer_message_turn_check = Mock()
    rt._enqueue_message_turn = Mock()
    rt.log_prefix = "[test]"

    sched = MessageTurnScheduler(rt)
    sched._dynamic_reply_gate = Mock()
    sched._dynamic_reply_gate.evaluate.return_value = SimpleNamespace(decision="trigger", detail="")

    # 1. 下载进行中 -> 门控推迟 0.5s，不触发入队
    sched.schedule_message_turn()
    rt._defer_message_turn_check.assert_called_once_with(0.5)
    rt._enqueue_message_turn.assert_not_called()

    # 2. 任务变为 ready -> 立刻放行入队
    con = sqlite3.connect(db_path)
    con.execute("UPDATE message_media_jobs SET state='ready' WHERE msg_id='143404'")
    con.commit()
    con.close()

    rt._defer_message_turn_check.reset_mock()
    sched.schedule_message_turn()
    rt._defer_message_turn_check.assert_not_called()
    rt._enqueue_message_turn.assert_called_once()

    # 3. 持续 running 超过上限（10.0s）-> 强制放行，永不卡死
    img_msg2 = _make_wechat_msg("[图片]", msg_id="wechat:s1:acc1:143499", wechat_msg_id="143499", wechat_msg_type=3)
    rt.message_cache = [img_msg2]
    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT INTO message_media_jobs VALUES ('s1', 'acc1', '45009933541@chatroom', '143499', 0, 'waiting_file', 1000.0)"
    )
    con.commit()
    con.close()
    assert rt._get_media_hydration_wait_seconds() == pytest.approx(0.5)
    now_box[0] = 110.5
    assert rt._get_media_hydration_wait_seconds() == 0.0

    # 4. 普通文字引用回复（type=49，无 media_jobs 记录）不应空等
    quote_msg = _make_wechat_msg("普通引用文字", msg_id="wechat:s1:acc1:143500", wechat_msg_id="143500", wechat_msg_type=49)
    rt.message_cache = [quote_msg]
    assert rt._get_media_hydration_wait_seconds() == 0.0


# ---------------------------------------------------------------------------
# P1-1: 整批双子路由切槽 + 零侵入 MVU 系统只读护栏
# ---------------------------------------------------------------------------


def _make_profiles():
    younger = SimpleNamespace(nickname="许妍", alias_names=["妍妍", "妹妹"])
    elder = SimpleNamespace(nickname="许玥", alias_names=["玥姐", "阿玥"])
    pers = SimpleNamespace(
        twin_mode_enabled=True,
        resolve_slot_profile=lambda slot, *_a: elder if slot == "sister_elder" else younger,
    )
    bot = SimpleNamespace(nickname="许妍", alias_names=["许玥"])
    eco = SimpleNamespace(
        enabled=True,
        chat_execution_mode="one_pass_st",
        hibernating_threshold_usd=0.5,
        thrifty_threshold_usd=2.0,
        supercharged_threshold_usd=5.0,
        away_evolve_interval_hours=3.0,
    )
    return eco, pers, bot


@pytest.mark.asyncio
async def test_twin_router_scans_entire_batch_for_elder_switch_and_mvu_readonly_untouched(tmp_path: Path) -> None:
    eco, pers, bot = _make_profiles()
    truth_db = tmp_path / "truth.db"
    ledger = ComputeLedger(truth_db)
    ledger._remote_balance_usd = 2.84
    ledger._is_available = True

    # 场景 1：复现线上 Cycle 2（先 @许妍，再说让姐姐看，末尾补 [合十][合十]）-> 应准确切到 sister_elder
    d_elder = await resolve_turn_route(
        session_id="grp1",
        platform="wechat",
        sender_id="wxid_paff",
        is_group_chat=True,
        latest_user_text="[合十][合十]",
        batch_user_texts=["@许妍 这道题你怎么看？", "感觉好难让你姐姐看一下", "[合十][合十]"],
        mentions_bot=True,
        ledger=ledger,
        truth_db_path=str(truth_db),
        eco_cfg=eco,
        personality_cfg=pers,
        bot_cfg=bot,
    )
    assert d_elder.compute_tier == "STANDARD"
    assert d_elder.forced_slot is None
    assert d_elder.recommended_slot == "sister_elder"

    # 场景 2：同一批次先叫姐姐，后改口叫许妍 -> 以最后一次显式点名为准，切回 sister_younger
    d_back_younger = await resolve_turn_route(
        session_id="grp1",
        platform="wechat",
        sender_id="wxid_paff",
        is_group_chat=True,
        latest_user_text="算了许妍你来吧",
        batch_user_texts=["姐姐在吗", "算了许妍你来吧"],
        ledger=ledger,
        truth_db_path=str(truth_db),
        eco_cfg=eco,
        personality_cfg=pers,
        bot_cfg=bot,
    )
    assert d_back_younger.recommended_slot == "sister_younger"

    # 场景 3：单条消息同时含“许妍”和“姐姐” -> 优先 sister_elder
    d_same_msg = await resolve_turn_route(
        session_id="grp1",
        platform="wechat",
        sender_id="wxid_paff",
        is_group_chat=True,
        latest_user_text="许妍，让你姐姐来看一下这道题",
        ledger=ledger,
        truth_db_path=str(truth_db),
        eco_cfg=eco,
        personality_cfg=pers,
        bot_cfg=bot,
    )
    assert d_same_msg.recommended_slot == "sister_elder"

    # 场景 4：省流态（THRIFTY）下末尾补了 [合十]，依然能准确捕捉前序“姐姐”并注入省流期唤姐应答指令
    ledger._remote_balance_usd = 1.50
    ledger.set_elder_away_arc("姐姐去研究室跑实验了")
    d_thrifty = await resolve_turn_route(
        session_id="grp1",
        platform="wechat",
        sender_id="wxid_paff",
        is_group_chat=True,
        latest_user_text="[合十][合十]",
        batch_user_texts=["让你姐姐看一下", "[合十][合十]"],
        ledger=ledger,
        truth_db_path=str(truth_db),
        eco_cfg=eco,
        personality_cfg=pers,
        bot_cfg=bot,
    )
    assert d_thrifty.compute_tier == "THRIFTY"
    assert d_thrifty.forced_slot == "sister_younger"
    assert "省流期唤姐应答指令" in d_thrifty.transient_narrative_note

    # 场景 5：确认 MVU 系统只读护栏零改动、依然完好
    assert MVURuntimeEngine.SYSTEM_READONLY_KEYS == ("balance_tier", "phone_holder")
