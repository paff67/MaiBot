from pathlib import Path
import json
import sqlite3

import pytest

from src.common.data_models.message_component_data_model import AtComponent, TextComponent
from src.companion_vault.truth_db import CompanionTruthDB
from src.platform_io.wechat.projection import (
    BOT_SELF_WXID,
    build_identity_lookup_maps,
    enrich_record_from_truth_db,
    enrich_sender_identity,
    extract_xml_at_wxids,
    parse_wechat_text_to_raw_segments,
    project_message,
    strip_group_sender_prefix,
)
from src.plugin_runtime.host.message_utils import PluginMessageUtils


def test_parse_wechat_text_to_raw_segments_and_plugin_message_utils_deserialization():
    """断言 1：中间字典段与 PluginMessageUtils 反序列化兼容性"""
    raw_text = "wxid_6zag123:\n@ì _eun\u2005 出来玩"
    xml_content = "<atuserlist><![CDATA[wxid_ma7dkr0z5nnb22]]></atuserlist>"

    xml_at_wxids = extract_xml_at_wxids(xml_content)
    assert xml_at_wxids == ["wxid_ma7dkr0z5nnb22"]

    wxid_to_profile = {
        "wxid_ma7dkr0z5nnb22": {
            "person_id": "P0001",
            "person_name": "ì _eun",
            "wechat_remark": "R",
            "trust_role": "GROUP_MEMBER",
        }
    }
    name_to_wxid = {
        "ì _eun": "wxid_ma7dkr0z5nnb22",
        "r": "wxid_ma7dkr0z5nnb22",
    }

    segments = parse_wechat_text_to_raw_segments(
        raw_text,
        sender_id="wxid_6zag123",
        is_group=True,
        wxid_to_profile=wxid_to_profile,
        name_to_wxid=name_to_wxid,
        xml_at_wxids=xml_at_wxids,
    )

    expected = [
        {
            "type": "at",
            "data": {
                "user_id": "wxid_ma7dkr0z5nnb22",
                "user_nickname": "ì _eun(备注:R)",
            },
        },
        {
            "type": "text",
            "data": " 出来玩",
        },
    ]
    assert segments == expected

    # 传入 PluginMessageUtils._message_sequence_from_dict 反序列化
    seq = PluginMessageUtils._message_sequence_from_dict(segments)
    assert len(seq.components) == 2

    at_comp = seq.components[0]
    assert isinstance(at_comp, AtComponent)
    assert at_comp.target_user_id == "wxid_ma7dkr0z5nnb22"
    assert at_comp.target_user_nickname == "ì _eun(备注:R)"

    text_comp = seq.components[1]
    assert isinstance(text_comp, TextComponent)
    assert text_comp.text == " 出来玩"


def test_enrich_sender_identity_preserves_nickname_and_remark():
    """断言 2：发言者昵称与备注名双保留断言"""
    enriched: dict[str, str] = {}
    wxid_to_profile = {
        "wxid_ma7dkr0z5nnb22": {
            "person_id": "P0001",
            "person_name": "ì _eun",
            "wechat_remark": "R",
            "trust_role": "GROUP_MEMBER",
        }
    }

    enrich_sender_identity(enriched, "wxid_ma7dkr0z5nnb22", wxid_to_profile)

    assert enriched["sender_nickname"] == "ì _eun"
    assert enriched["sender_cardname"] == "R"
    assert enriched["sender_person_name"] == "ì _eun"
    assert enriched["sender_wechat_remark"] == "R"


def test_build_planner_catalog_injection_backward_compat_and_group_rbac(tmp_path: Path):
    """断言 3：build_planner_catalog_injection 向下兼容与群聊 RBAC 脱敏断言"""
    db_path = tmp_path / "companion_truth.db"
    truth_db = CompanionTruthDB(str(db_path))

    # 在 person_info 中写入包含私密 impression 与 birthday 的记录
    try:
        truth_db.conn.execute("ALTER TABLE person_info ADD COLUMN birthday TEXT")
    except sqlite3.OperationalError:
        pass

    with truth_db.conn:
        truth_db.conn.execute(
            """
            INSERT OR REPLACE INTO person_info(
                person_id, platform_ids, person_name, wechat_remark, trust_role, impression, birthday
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "P0001",
                json.dumps({"wechat": "wxid_ma7dkr0z5nnb22"}),
                "ì _eun",
                "R",
                "GROUP_MEMBER",
                "这是绝对机密的私人评价",
                "1999-01-01",
            ),
        )

    # 使用旧签名调用（不传新参数 chat_id / active_sender_ids）
    catalog = truth_db.build_planner_catalog_injection(
        is_group_chat=True,
        sender_trust_role="GROUP_MEMBER",
    )

    # 验证包含自身的账号与提及判定准则
    assert "<自身账号与提及判定准则>" in catalog
    assert BOT_SELF_WXID in catalog

    # 验证群聊重要角色索引包含公开脱敏名册行
    assert "[角色ID: P0001] 备注名: R | 微信昵称: ì _eun | 身份: GROUP_MEMBER" in catalog

    # 验证绝对不泄露私密 impression 与 birthday
    assert "这是绝对机密的私人评价" not in catalog
    assert "1999-01-01" not in catalog

    truth_db.close()


def test_project_message_end_to_end_with_at_mention_and_session_message(tmp_path: Path):
    """断言 4（端到端整合）：从真实仿真账本记录到 SessionMessage 的全链条流转"""
    db_path = tmp_path / "companion_truth.db"
    truth_db = CompanionTruthDB(str(db_path))
    with truth_db.conn:
        truth_db.conn.execute(
            """
            INSERT INTO person_info(person_id, platform_ids, person_name, wechat_remark, trust_role)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("P0001", json.dumps({"wechat": "wxid_ma7dkr0z5nnb22"}), "ì _eun", "R", "GROUP_MEMBER"),
        )
    truth_db.close()

    raw_xml = "<msgsource><atuserlist><![CDATA[wxid_ma7dkr0z5nnb22]]></atuserlist></msgsource>"
    row_json = {
        "msgId": "10001",
        "talker": "1234567@chatroom",
        "type": 1,
        "createTime": 1700000000000,
        "content": "wxid_6zag123:\n@ì _eun\u2005 出来玩",
        "msg_source": raw_xml,
    }
    record = {
        "source": "/data/data/com.tencent.mm",
        "account": "1234567890abcdef1234567890abcdef",
        "chat_id": "1234567@chatroom",
        "msg_id": "10001",
        "direction": "incoming",
        "sender_id": "wxid_6zag123",
        "body": "wxid_6zag123:\n@ì _eun\u2005 出来玩",
        "kind": 1,
        "revision": "rev_001",
        "row_json": json.dumps(row_json),
        "raw_content": raw_xml,
    }

    enriched = enrich_record_from_truth_db(record, str(db_path))
    payload = project_message(enriched)

    assert payload["platform"] == "wechat"
    assert payload["message_info"]["group_info"]["group_id"] == "1234567@chatroom"
    assert len(payload["raw_message"]) == 2
    assert payload["raw_message"][0]["type"] == "at"
    assert payload["raw_message"][0]["data"]["user_id"] == "wxid_ma7dkr0z5nnb22"
    assert payload["raw_message"][0]["data"]["user_nickname"] == "ì _eun(备注:R)"
    assert payload["raw_message"][1]["type"] == "text"
    assert payload["raw_message"][1]["data"] == " 出来玩"

    session_msg = PluginMessageUtils._build_session_message_from_dict(payload)
    assert len(session_msg.raw_message.components) == 2
    assert isinstance(session_msg.raw_message.components[0], AtComponent)
    assert session_msg.raw_message.components[0].target_user_id == "wxid_ma7dkr0z5nnb22"
    assert session_msg.raw_message.components[0].target_user_nickname == "ì _eun(备注:R)"
    assert isinstance(session_msg.raw_message.components[1], TextComponent)
    assert session_msg.raw_message.components[1].text == " 出来玩"
