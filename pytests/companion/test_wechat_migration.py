import asyncio
import json
import sys

import httpx
import pytest

from src.platform_io.wechat.driver import WeChatRootDriver
from src.platform_io.wechat.edge_client import WeChatEdgeClient
from src.platform_io.wechat.projection import native_message_id, project_message
from src.platform_io.wechat.message_ingestor import MessageLedger
from src.companion_vault.wechat_recall import quote_binding

ACCOUNT = "a" * 32
CHAT = "group@chatroom"
SOURCE = "http://edge"


def record(mid="1", kind="1", body="hello"):
    row = dict(
        msgId=mid,
        talker=CHAT,
        type=kind,
        content=body,
        createTime="1793880000000",
        isSend="0",
        msgSvrId="9007199254740993",
    )
    return dict(
        source=SOURCE,
        account=ACCOUNT,
        msg_id=mid,
        chat_id=CHAT,
        revision="r1",
        direction="incoming",
        sender_id="wxid_test",
        body=body,
        row_json=json.dumps(row),
    )


def test_projection_stable_identity_and_no_model_import():
    a = project_message(record())
    b = project_message(record("2"))
    assert a["message_id"] != b["message_id"]
    assert a["account_id"] == ACCOUNT and a["message_info"]["group_info"]["group_id"] == CHAT
    assert a["timestamp"] == 1793880000
    assert "session_id" not in a
    assert native_message_id("http://other", ACCOUNT, "1") != a["message_id"]
    for changes in [dict(direction="outgoing"), dict(sender_id=None), dict(chat_id="other")]:
        with pytest.raises(ValueError):
            project_message(dict(record(), **changes))
    with pytest.raises(ValueError):
        project_message(record(kind="268445456"))


def test_quote_binding_uses_database_not_untrusted_metadata(tmp_path):
    path = tmp_path / "ledger.db"
    ledger = MessageLedger(str(path))
    content = "<msg><appmsg><type>57</type><title>看这个</title><refermsg><type>1</type><svrid>9007199254740993</svrid><fromusr>group@chatroom</fromusr><content>old</content></refermsg></appmsg></msg>"
    r = record("2", "822083633", content)
    ledger.ingest(
        SOURCE,
        "epoch",
        dict(
            seq=1,
            account=ACCOUNT,
            msg_id="2",
            chat_id=CHAT,
            revision="r1",
            observed_at_ms=1,
            row=json.loads(r["row_json"]),
        ),
    )
    ledger.db.close()
    payload = project_message(r)
    assert payload["raw_message"] == [{"type": "text", "data": "看这个"}]
    args = dict(
        message_id=payload["message_id"], platform="wechat", account=ACCOUNT, chat_id=CHAT, ledger_path=str(path)
    )
    assert quote_binding(**args)["quote_message_ids"] == ["2"]
    assert quote_binding(**dict(args, chat_id="other@chatroom")) == {}
    assert quote_binding(**dict(args, message_id=native_message_id("http://other", ACCOUNT, "2"))) == {}


def test_native_driver_acceptance_does_not_mean_completed_turn():
    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
        edge = WeChatEdgeClient(SOURCE, ACCOUNT, client=client)
        driver = WeChatRootDriver(edge, allowed_chats=[CHAT])
        received = []

        async def accept(envelope):
            received.append(envelope)
            return True

        driver.set_inbound_handler(accept)
        assert await driver.deliver_committed(record())
        assert received[0].payload["message_id"] == native_message_id(SOURCE, ACCOUNT, "1")
        assert not await driver.deliver_committed(dict(record(), chat_id="other"))
        with pytest.raises(ValueError):
            await driver.deliver_committed(dict(record(), source="http://other"))
        await client.aclose()

    asyncio.run(run())


def test_root_transport_never_sends_into_other_chat():
    async def run():
        calls = []

        def handle(request):
            calls.append(request.method)
            return httpx.Response(200, json=dict(protocol_version=2, account=ACCOUNT, chat_id="other@chatroom"))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            edge = WeChatEdgeClient(SOURCE, ACCOUNT, client=client)
            with pytest.raises(ValueError):
                await edge.send_text(CHAT, "hello", "reply1")
        assert calls == ["GET"]

    asyncio.run(run())


def test_package_import_is_lightweight():
    import subprocess

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import src.platform_io.wechat.projection; assert 'src.config.config' not in sys.modules; assert 'src.maisaka.reasoning_engine' not in sys.modules",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_old_self_bubble_is_not_confirmed_as_new_send():
    xml = '<hierarchy><node content-desc="麦麦头像" bounds="[960,300][1060,400]"/><node text="hello" bounds="[650,310][920,390]"/><node class="android.widget.EditText" bounds="[200,2000][800,2100]"/></hierarchy>'

    async def run():
        posts = []

        def handle(request):
            if request.method == "POST":
                posts.append(json.loads(request.content))
                return httpx.Response(200, json={"ok": True, "sent": True})
            return httpx.Response(200, json=dict(protocol_version=2, account=ACCOUNT, chat_id=CHAT, xml=xml))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            edge = WeChatEdgeClient(SOURCE, ACCOUNT, client=client)
            a = await edge.send_text(CHAT, "hello", "stable-reply")
            b = await edge.send_text(CHAT, "hello", "stable-reply")
        assert a["status"] == b["status"] == "submitted_unconfirmed"
        assert posts[0]["request_id"] == posts[1]["request_id"]

    asyncio.run(run())


def test_hook_send_background_without_foreground_ui():
    async def run():
        calls = []

        def handle(request):
            calls.append((request.method, request.url.path))
            assert request.url.path == "/wechat/send_text"
            body = json.loads(request.content)
            assert body["expected_account"] == ACCOUNT
            assert body["expected_chat_id"] == CHAT
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "sent": True,
                    "status": "confirmed",
                    "request_id": body["request_id"],
                    "msg_id": "555160",
                    "msg_svr_id": "8123456789012345678",
                    "create_time": "1793954000000",
                    "send_layer": "ISendMsgMgr:rt5.i5.Jj(via no3.w1.a,type=1,ppc=true)",
                    "chatting_context_talker": "none",
                    "typing_delay_ms": 1850,
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            edge = WeChatEdgeClient(SOURCE, ACCOUNT, client=client, use_hook_send=True, max_sends_per_minute=2)
            res = await edge.send_text(CHAT, "后台发送测试", "hook-reply-1")
            assert res["status"] == "confirmed"
            assert res["msg_id"] == "555160"
            assert res["msg_svr_id"] == "8123456789012345678"
            assert res["typing_delay_ms"] == 1850
            assert "rt5.i5.Jj" in res["send_layer"]
            # Same internal_id is idempotent at client rate limiter
            res_dup = await edge.send_text(CHAT, "后台发送测试", "hook-reply-1")
            assert res_dup["request_id"] == res["request_id"]
            # Second distinct message within 1 minute succeeds (2/2)
            await edge.send_text(CHAT, "第二条消息", "hook-reply-2")
            # Third distinct message within 1 minute is blocked by client per-minute rate limit
            with pytest.raises(ValueError, match="rate limit"):
                await edge.send_text(CHAT, "第三条超限消息", "hook-reply-3")
        # Verify GET /wechat/chat_snapshot was NEVER called
        assert all(path == "/wechat/send_text" for _, path in calls)

    asyncio.run(run())


def test_typing_delay_and_edge_rate_limit_window(tmp_path):
    from src.phone_edge.send_ledger import RateLimitExceededError, SendLedger, compute_typing_delay_ms
    from src.phone_edge.message_journal import enqueue_send_spool, read_send_outbox
    import sqlite3

    short_delays = [compute_typing_delay_ms("好", min_delay_ms=800, max_delay_ms=10000) for _ in range(20)]
    long_delays = [compute_typing_delay_ms("这是一段比较长的拟人打字延迟测试文本内容" * 2, min_delay_ms=800, max_delay_ms=10000) for _ in range(20)]
    assert all(800 <= d <= 10000 for d in short_delays + long_delays)
    assert sum(long_delays) / len(long_delays) > sum(short_delays) / len(short_delays)

    ledger = SendLedger(str(tmp_path / "send_requests.db"))
    slot1 = ledger.check_and_acquire_rate_slot("req_00000001", CHAT, max_per_minute=2, now_ms=100_000)
    slot2 = ledger.check_and_acquire_rate_slot("req_00000002", CHAT, max_per_minute=2, now_ms=110_000)
    assert slot1["window_count"] == 1 and slot2["window_count"] == 2
    with pytest.raises(RateLimitExceededError) as exc_info:
        ledger.check_and_acquire_rate_slot("req_00000003", CHAT, max_per_minute=2, now_ms=120_000)
    assert exc_info.value.count == 2 and exc_info.value.limit == 2 and exc_info.value.retry_after_ms == 40_000
    # After 60s window slides past req_00000001 (at 160_001ms), a new send is allowed
    slot3 = ledger.check_and_acquire_rate_slot("req_00000003", CHAT, max_per_minute=2, now_ms=160_001)
    assert slot3["window_count"] == 2

    spool_file = enqueue_send_spool(
        {"request_id": "req_00000001", "account": ACCOUNT, "chat_id": CHAT, "content": "hi"},
        spool_dir=str(tmp_path / "spool"),
    )
    assert spool_file.is_file()
    journal_path = str(tmp_path / "journal.db")
    with sqlite3.connect(journal_path) as db:
        db.execute(
            "CREATE TABLE send_outbox(request_id TEXT PRIMARY KEY,account TEXT NOT NULL,chat_id TEXT NOT NULL,"
            "content TEXT NOT NULL,status TEXT NOT NULL,created_at_ms INTEGER NOT NULL,updated_at_ms INTEGER NOT NULL,"
            "msg_id TEXT,msg_svr_id TEXT,create_time TEXT,send_layer TEXT,chatting_context_talker TEXT,error TEXT)"
        )
        db.execute(
            "INSERT INTO send_outbox VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("req_00000001", ACCOUNT, CHAT, "hi", "confirmed", 100, 200, "555160", "99999", "1793954000000", "rt5.i5.Jj", "none", None),
        )
    out = read_send_outbox("req_00000001", path=journal_path)
    assert out is not None and out["status"] == "confirmed" and out["msg_id"] == "555160"


def test_route_c_ledger_consumer_and_truth_enrichment(tmp_path):
    from src.companion_vault.truth_db import CompanionTruthDB

    ledger_path = str(tmp_path / "ledger.db")
    truth_path = str(tmp_path / "truth.db")
    ledger = MessageLedger(ledger_path)

    # Historical message before driver cursor initialization (should not backfill by default)
    ledger.ingest(
        SOURCE,
        "epoch",
        dict(
            seq=1,
            account=ACCOUNT,
            msg_id="1",
            chat_id=CHAT,
            revision="r1",
            observed_at_ms=1,
            row=dict(
                msgId="1",
                talker=CHAT,
                type="1",
                content="wxid_test:\n历史旧消息",
                msgSvrId="1001",
                isSend="0",
                createTime="1793880000000",
            ),
        ),
    )

    truth = CompanionTruthDB(truth_path)
    with truth.conn:
        truth.conn.execute(
            "INSERT INTO person_info(person_id, person_name, wechat_remark, trust_role, platform_ids) "
            "VALUES ('p_test', '小明', '明哥', 'GROUP_MEMBER', '{\"wechat\":\"wxid_test\"}')"
        )
    truth.close()

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200)))
        edge = WeChatEdgeClient(SOURCE, ACCOUNT, client=client, use_hook_send=True)
        driver = WeChatRootDriver(
            edge,
            allowed_chats=[CHAT],
            ledger_path=ledger_path,
            truth_path=truth_path,
            poll_interval=0.05,
            backfill=False,
        )
        envelopes = []

        async def accept(env):
            envelopes.append(env)
            return True

        driver.set_inbound_handler(accept)
        # First consume initializes cursor at max(rowid)=1 without replaying historical msg_id=1
        assert await driver.ledger_source.consume_once(driver) == 0
        assert envelopes == []

        # New incoming message (seq=2, msg_id=2)
        ledger.ingest(
            SOURCE,
            "epoch",
            dict(
                seq=2,
                account=ACCOUNT,
                msg_id="2",
                chat_id=CHAT,
                revision="r2",
                observed_at_ms=2,
                row=dict(
                    msgId="2",
                    talker=CHAT,
                    type="1",
                    content="wxid_test:\n实时新消息",
                    msgSvrId="1002",
                    isSend="0",
                    createTime="1793880005000",
                ),
            ),
        )
        # Subsequent revision on same msg_id=2 + outgoing message msg_id=3
        ledger.ingest(
            SOURCE,
            "epoch",
            dict(
                seq=3,
                account=ACCOUNT,
                msg_id="2",
                chat_id=CHAT,
                revision="r3",
                observed_at_ms=3,
                row=dict(
                    msgId="2",
                    talker=CHAT,
                    type="1",
                    content="wxid_test:\n实时新消息",
                    msgSvrId="1002",
                    isSend="0",
                    createTime="1793880005000",
                ),
            ),
        )
        ledger.ingest(
            SOURCE,
            "epoch",
            dict(
                seq=4,
                account=ACCOUNT,
                msg_id="3",
                chat_id=CHAT,
                revision="r4",
                observed_at_ms=4,
                row=dict(
                    msgId="3",
                    talker=CHAT,
                    type="1",
                    content="我自己的回复",
                    msgSvrId="1003",
                    isSend="1",
                    createTime="1793880006000",
                ),
            ),
        )

        await driver.start()
        await asyncio.sleep(0.15)
        await driver.stop()

        assert len(envelopes) == 1
        user_info = envelopes[0].payload["message_info"]["user_info"]
        assert user_info["user_id"] == "wxid_test"
        assert user_info["user_nickname"] == "明哥"
        assert user_info["user_cardname"] == "明哥"

    try:
        asyncio.run(run())
    finally:
        ledger.db.close()


def test_driver_send_message_tolerates_reply_component_and_formats_text(tmp_path):
    from types import SimpleNamespace
    from src.platform_io.types import DeliveryStatus
    from src.platform_io.wechat.driver import configured_wechat_drivers

    async def run():
        sent_payloads = []

        def handle(request):
            body = json.loads(request.content)
            sent_payloads.append(body)
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "sent": True,
                    "status": "confirmed",
                    "request_id": body["request_id"],
                    "msg_id": "143001",
                    "msg_svr_id": "4199476097781321913",
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            edge = WeChatEdgeClient(SOURCE, ACCOUNT, client=client, use_hook_send=True)
            driver = WeChatRootDriver(edge, allowed_chats=[CHAT], send_enabled=True)
            msg = SimpleNamespace(
                message_id="send_api_1001",
                message_info=SimpleNamespace(
                    group_info=SimpleNamespace(group_id=CHAT),
                    additional_config={},
                ),
                raw_message=SimpleNamespace(
                    components=[
                        SimpleNamespace(format_name="reply", target_message_id="wechat:1"),
                        SimpleNamespace(format_name="text", text="<think>内部思考</think>**你好呀**"),
                    ]
                ),
            )
            receipt = await driver.send_message(msg, driver.descriptor.route_key)
            assert receipt.status == DeliveryStatus.SENT
            assert receipt.external_message_id == "143001"
            assert sent_payloads[0]["text"] == "你好呀"

            # Unsupported ImageComponent must still be rejected
            bad_msg = SimpleNamespace(
                message_id="send_api_1002",
                message_info=SimpleNamespace(
                    group_info=SimpleNamespace(group_id=CHAT),
                    additional_config={},
                ),
                raw_message=SimpleNamespace(
                    components=[
                        SimpleNamespace(format_name="text", text="带图"),
                        SimpleNamespace(format_name="image", binary_hash="abc", binary_data=b"123"),
                    ]
                ),
            )
            bad_receipt = await driver.send_message(bad_msg, driver.descriptor.route_key)
            assert bad_receipt.status == DeliveryStatus.FAILED

    asyncio.run(run())

    toml_file = tmp_path / "wechat_driver.toml"
    toml_file.write_text(
        f'version = 1\ntruth_path = "data/truth.db"\nledger_path = "data/ledger.db"\n'
        f'[[drivers]]\nenabled = true\nsource = "{SOURCE}"\naccount = "{ACCOUNT}"\n'
        f'allowed_chats = ["{CHAT}"]\nsend_enabled = true\nuse_hook_send = true\nmax_sends_per_minute = 5\n',
        encoding="utf-8",
    )
    drivers = configured_wechat_drivers(str(toml_file))
    assert len(drivers) == 1
    assert drivers[0].edge.use_hook_send is True and drivers[0].edge.max_sends_per_minute == 5
    assert CHAT in drivers[0].allowed_chats
    asyncio.run(drivers[0].stop())


def test_ledger_quarantine_and_manager_synchronous_dispatch_rollback(tmp_path):
    from types import SimpleNamespace
    from src.platform_io.wechat.ledger_source import LedgerSource
    from src.platform_io.wechat.projection import enrich_record_from_truth_db
    from src.platform_io.manager import PlatformIOManager
    from src.platform_io.types import DriverDescriptor, DriverKind, InboundMessageEnvelope, RouteBinding, RouteKey
    from src.platform_io.drivers.base import PlatformIODriver

    ledger_path = str(tmp_path / "ledger.db")
    ledger = MessageLedger(ledger_path)
    # 1. Poison incoming group message without sender
    ledger.ingest(
        SOURCE,
        "epoch",
        dict(
            seq=1,
            account=ACCOUNT,
            msg_id="1",
            chat_id=CHAT,
            revision="r1",
            observed_at_ms=1,
            row=dict(
                msgId="1",
                talker=CHAT,
                type="1",
                content="malformed_no_sender_prefix",
                msgSvrId="2001",
                isSend="0",
                createTime="1793880000000",
            ),
        ),
    )
    # 2. Valid incoming group message following poison message
    ledger.ingest(
        SOURCE,
        "epoch",
        dict(
            seq=2,
            account=ACCOUNT,
            msg_id="2",
            chat_id=CHAT,
            revision="r2",
            observed_at_ms=2,
            row=dict(
                msgId="2",
                talker=CHAT,
                type="1",
                content="wxid_valid:\n正常后续消息",
                msgSvrId="2002",
                isSend="0",
                createTime="1793880001000",
            ),
        ),
    )
    # 3. Quote message referencing msg_id=2 with fromusr=wxid_valid
    quote_xml = (
        "<msg><appmsg><type>57</type><title>引用回复正文</title>"
        "<refermsg><type>1</type><svrid>2002</svrid><fromusr>wxid_valid</fromusr>"
        "<chatusr>wxid_valid</chatusr><displayname>群友V</displayname>"
        "<content>正常后续消息</content></refermsg></appmsg></msg>"
    )
    ledger.ingest(
        SOURCE,
        "epoch",
        dict(
            seq=3,
            account=ACCOUNT,
            msg_id="3",
            chat_id=CHAT,
            revision="r3",
            observed_at_ms=3,
            row=dict(
                msgId="3",
                talker=CHAT,
                type="822083633",
                content="wxid_replier:\n" + quote_xml,
                msgSvrId="2003",
                isSend="0",
                createTime="1793880002000",
            ),
        ),
    )

    seen = []

    async def deliver(row):
        enriched = enrich_record_from_truth_db(row, None, ledger_path=ledger_path)
        projected = project_message(enriched)
        seen.append((row["msg_id"], projected))
        return True

    consumer = LedgerSource(ledger_path, SOURCE, ACCOUNT, backfill=True)
    driver = SimpleNamespace(driver_id="test_drv", allowed_chats={CHAT}, backfill=True, deliver_committed=deliver)
    delivered_count = asyncio.run(consumer.consume_once(driver))
    assert delivered_count == 2
    assert [m[0] for m in seen] == ["2", "3"]
    # Verify msg_id=3 projected a structured reply segment pointing to msg_id=2
    assert seen[1][1]["raw_message"][0] == {
        "type": "reply",
        "data": native_message_id(SOURCE, ACCOUNT, "2"),
    }
    assert seen[1][1]["raw_message"][1] == {"type": "text", "data": "引用回复正文"}
    # Verify msg_id=1 was recorded in wechat_driver_quarantine
    q_row = ledger.db.execute(
        "SELECT msg_id, error FROM wechat_driver_quarantine WHERE driver_id='test_drv'"
    ).fetchone()
    assert q_row is not None and q_row[0] == "1" and "Unknown sender" in q_row[1]
    ledger.db.close()

    # Verify PlatformIOManager.accept_inbound awaits dispatcher and rolls back deduplicator on failure
    class DummyDriver(PlatformIODriver):
        async def send_message(self, message, route_key, metadata=None):
            raise NotImplementedError

    async def verify_manager():
        mgr = PlatformIOManager()
        rk = RouteKey(platform="wechat", account_id=ACCOUNT, scope="s1")
        drv = DummyDriver(DriverDescriptor(driver_id="d1", kind=DriverKind.LOCAL, platform="wechat", account_id=ACCOUNT, scope="s1"))
        mgr.register_driver(drv)
        mgr.bind_receive_route(RouteBinding(route_key=rk, driver_id="d1", driver_kind=DriverKind.LOCAL))
        attempts = 0

        async def flaky_dispatcher(env):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("transient dispatch error")

        mgr.set_inbound_dispatcher(flaky_dispatcher)
        env = InboundMessageEnvelope(
            route_key=rk,
            driver_id="d1",
            driver_kind=DriverKind.LOCAL,
            external_message_id="m_100",
            payload={"message_id": "m_100"},
        )
        assert await mgr.accept_inbound(env) is False
        assert await mgr.accept_inbound(env) is True
        assert attempts == 2

    asyncio.run(verify_manager())


def test_multi_conversation_private_fallback_rate_backoff_and_scope_sync(tmp_path):
    from types import SimpleNamespace
    import time
    from src.companion_vault.consolidator import configured_consolidation_tasks
    from src.platform_io.types import DeliveryStatus

    # 1. Verify configured_consolidation_tasks syncs allowed_chats from wechat_driver.toml
    driver_toml = tmp_path / "wechat_driver.toml"
    driver_toml.write_text(
        f'version = 1\ntruth_path = "data/truth.db"\nledger_path = "data/ledger.db"\n'
        f'[[drivers]]\nenabled = true\nsource = "{SOURCE}"\naccount = "{ACCOUNT}"\n'
        f'allowed_chats = ["{CHAT}", "999888777@chatroom", "wxid_friend_dm"]\n'
        f'send_enabled = true\nuse_hook_send = true\nmax_sends_per_minute = 15\n'
        f'max_sends_per_chat_per_minute = 6\nrate_limit_wait_seconds = 12.0\n',
        encoding="utf-8",
    )
    mem_toml = tmp_path / "memory.toml"
    mem_toml.write_text(
        f'version = 1\ntruth_path = "data/truth.db"\nledger_path = "data/ledger.db"\n'
        f'driver_config_path = "{driver_toml.as_posix()}"\n'
        f'[[scopes]]\nenabled = true\nsource = "{SOURCE}"\naccount = "{ACCOUNT}"\n'
        f'chat_id = "{CHAT}"\nmodel_task = "utils"\nbatch_size = 20\nbackfill = false\n',
        encoding="utf-8",
    )
    tasks = configured_consolidation_tasks(str(mem_toml))
    assert [t["chat_id"] for t in tasks] == [CHAT, "999888777@chatroom", "wxid_friend_dm"]

    # 2. Verify private chat outbound fallback & per-chat / queued backoff rate limit
    async def run():
        sent_targets = []

        def handle(request):
            body = json.loads(request.content)
            sent_targets.append(body["expected_chat_id"])
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "sent": True,
                    "status": "confirmed",
                    "request_id": body["request_id"],
                    "msg_id": "200001",
                    "msg_svr_id": "9990001",
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            edge = WeChatEdgeClient(
                SOURCE,
                ACCOUNT,
                client=client,
                use_hook_send=True,
                max_sends_per_minute=4,
                max_sends_per_chat_per_minute=1,
                rate_limit_wait_seconds=1.5,
            )
            driver = WeChatRootDriver(
                edge,
                allowed_chats=[CHAT, "wxid_friend_dm"],
                send_enabled=True,
            )
            # Private message where group_info is None and additional_config is empty -> falls back to user_info.user_id
            dm_msg = SimpleNamespace(
                message_id="dm_out_1",
                message_info=SimpleNamespace(
                    group_info=None,
                    user_info=SimpleNamespace(user_id="wxid_friend_dm"),
                    additional_config={},
                ),
                raw_message=SimpleNamespace(components=[SimpleNamespace(format_name="text", text="私聊回复1")]),
            )
            r1 = await driver.send_message(dm_msg, driver.descriptor.route_key)
            assert r1.status == DeliveryStatus.SENT
            assert sent_targets == ["wxid_friend_dm"]

            # Simulate first send occurred 59.6s ago so per-chat slot frees up in ~0.4s (< rate_limit_wait_seconds=1.5s)
            now_mono = time.monotonic()
            edge._send_timestamps[0] = now_mono - 59.6
            edge._chat_send_timestamps["wxid_friend_dm"][0] = now_mono - 59.6

            dm_msg_2 = SimpleNamespace(
                message_id="dm_out_2",
                message_info=SimpleNamespace(
                    group_info=None,
                    user_info=SimpleNamespace(user_id="wxid_friend_dm"),
                    additional_config={},
                ),
                raw_message=SimpleNamespace(components=[SimpleNamespace(format_name="text", text="私聊回复2")]),
            )
            r2 = await driver.send_message(dm_msg_2, driver.descriptor.route_key)
            assert r2.status == DeliveryStatus.SENT
            assert sent_targets == ["wxid_friend_dm", "wxid_friend_dm"]

    asyncio.run(run())

    # 3. Verify default wildcard ("*") mode auto-enables new friends & new groups + auto-syncs remarks & members
    from src.companion_vault.consolidator import _discover_ledger_chat_ids
    from src.companion_vault.truth_db import CompanionTruthDB

    wildcard_drv = tmp_path / "wechat_wildcard.toml"
    wildcard_drv.write_text(
        f'version = 1\ntruth_path = "data/truth.db"\nledger_path = "data/ledger.db"\n'
        f'[[drivers]]\nenabled = true\nsource = "{SOURCE}"\naccount = "{ACCOUNT}"\n'
        f'send_enabled = true\nuse_hook_send = true\n',
        encoding="utf-8",
    )
    wildcard_mem = tmp_path / "memory_wildcard.toml"
    wildcard_mem.write_text(
        f'version = 1\ntruth_path = "data/truth.db"\nledger_path = "data/ledger.db"\n'
        f'driver_config_path = "{wildcard_drv.as_posix()}"\n'
        f'[[scopes]]\nenabled = true\nsource = "{SOURCE}"\naccount = "{ACCOUNT}"\n'
        f'chat_id = "*"\nmodel_task = "utils"\n',
        encoding="utf-8",
    )
    w_tasks = configured_consolidation_tasks(str(wildcard_mem))
    assert len(w_tasks) == 1 and w_tasks[0]["chat_id"] == "*"

    truth_path = str(tmp_path / "auto_sync_truth.db")
    ledger_path = str(tmp_path / "auto_sync_ledger.db")
    ledger = MessageLedger(ledger_path)
    for idx, (cid, raw_text) in enumerate(
        [
            ("wxid_new_friend", "你好我是新加的好友"),
            ("88888888@chatroom", "wxid_new_member:\n大家好这是新拉的群"),
            ("gh_official_account", "公众号推文"),
            ("notifymessage", "服务通知"),
        ],
        start=1,
    ):
        ledger.ingest(
            SOURCE,
            "ep_auto",
            dict(
                seq=idx,
                account=ACCOUNT,
                msg_id=str(idx),
                chat_id=cid,
                revision=f"rev_{idx}",
                observed_at_ms=1000 + idx,
                row=dict(
                    msgId=str(idx),
                    talker=cid,
                    type="1",
                    content=raw_text,
                    msgSvrId=str(9000 + idx),
                    isSend="0",
                    createTime=str(1793880000000 + idx * 1000),
                ),
            ),
        )
    ledger.db.close()
    discovered = _discover_ledger_chat_ids(ledger_path, SOURCE, ACCOUNT)
    assert set(discovered) == {"wxid_new_friend", "88888888@chatroom"}

    async def verify_auto_directory_sync():
        def mock_edge_handler(request):
            if request.url.path == "/wechat/group_members":
                gid = request.url.params.get("group_id")
                if gid == "__contacts__":
                    return httpx.Response(
                        200,
                        json={
                            "ok": True,
                            "account": ACCOUNT,
                            "group_id": "__contacts__",
                            "user_ids": ["wxid_new_friend"],
                            "contacts": {"wxid_new_friend": {"nickname": "小夏", "remark": "夏夏"}},
                            "complete": True,
                        },
                    )
                if gid == "88888888@chatroom":
                    return httpx.Response(
                        200,
                        json={
                            "ok": True,
                            "account": ACCOUNT,
                            "group_id": "88888888@chatroom",
                            "user_ids": ["wxid_new_member", "wxid_new_friend"],
                            "contacts": {
                                "88888888@chatroom": {"nickname": "音乐合成器交流群", "remark": ""},
                                "wxid_new_member": {"nickname": "阿杰", "remark": "吉他阿杰"},
                            },
                            "complete": True,
                        },
                    )
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(mock_edge_handler)) as client:
            edge = WeChatEdgeClient(SOURCE, ACCOUNT, client=client, use_hook_send=True)
            drv = WeChatRootDriver(
                edge,
                send_enabled=True,
                ledger_path=ledger_path,
                truth_path=truth_path,
                backfill=True,
            )
            received_envelopes = []

            async def on_inbound(env):
                received_envelopes.append(env)
                return True

            drv.set_inbound_handler(on_inbound)
            count = await drv.ledger_source.consume_once(drv)
            # Only the 2 human/group chats are accepted; gh_official_account and notifymessage are ignored
            assert count == 2
            assert len(received_envelopes) == 2
            dm_env, grp_env = received_envelopes[0], received_envelopes[1]
            assert dm_env.payload["message_info"]["user_info"]["user_nickname"] == "夏夏"
            assert dm_env.payload["message_info"]["group_info"] is None
            assert grp_env.payload["message_info"]["group_info"]["group_name"] == "音乐合成器交流群"
            assert grp_env.payload["message_info"]["user_info"]["user_nickname"] == "吉他阿杰"

            tdb = CompanionTruthDB(truth_path)
            members = [
                r[0]
                for r in tdb.conn.execute(
                    "SELECT user_id FROM group_members WHERE group_id='88888888@chatroom' ORDER BY user_id"
                ).fetchall()
            ]
            assert members == ["wxid_new_friend", "wxid_new_member"]
            tdb.close()

    asyncio.run(verify_auto_directory_sync())


def test_wechat_quote_reply_resolution_and_dispatch(tmp_path):
    import asyncio
    import sqlite3
    import httpx
    from src.platform_io.wechat.edge_client import WeChatEdgeClient
    from src.platform_io.wechat.driver import WeChatRootDriver
    from src.common.data_models.message_component_data_model import ReplyComponent, TextComponent

    ledger_path = str(tmp_path / "ledger.db")
    with sqlite3.connect(ledger_path) as conn:
        conn.execute(
            "CREATE TABLE messages(source TEXT,account TEXT,msg_id TEXT,chat_id TEXT NOT NULL,"
            "server_id TEXT,sender_id TEXT,direction TEXT,kind TEXT,body TEXT,raw_content TEXT,"
            "revision TEXT,row_json TEXT NOT NULL,updated_at_ms INTEGER,PRIMARY KEY(source,account,msg_id))"
        )
        conn.execute(
            "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "http://localhost:8765",
                ACCOUNT,
                "143229",
                CHAT,
                "9138388385056250904",
                "wxid_friend",
                "incoming",
                "1",
                "上次不是我吗",
                "上次不是我吗",
                "rev1",
                json.dumps({"createTime": 1794142175000}),
                1794142175000,
            ),
        )

    truth_path = str(tmp_path / "truth.db")
    with sqlite3.connect(truth_path) as conn:
        conn.execute(
            "CREATE TABLE person_info(person_id TEXT PRIMARY KEY, platform_ids TEXT DEFAULT '{}', person_name TEXT, wechat_remark TEXT)"
        )
        conn.execute(
            "INSERT INTO person_info VALUES(?, ?, ?, ?)",
            ("p1", '{"wechat": "wxid_friend"}', "FriendName", "FriendRemark"),
        )

    calls = []

    async def handle(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, json.loads(request.content.decode())))
        return httpx.Response(
            200,
            json={
                "ok": True,
                "sent": True,
                "status": "confirmed",
                "request_id": "quote-req-1",
                "msg_id": "143230",
                "msg_svr_id": "999888777666",
                "create_time": "1794143082828",
                "send_layer": "AppMsgLogic:k0.I(type=57,svrid=9138388385056250904)",
                "chatting_context_talker": "none",
                "typing_delay_ms": 300,
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            edge = WeChatEdgeClient(SOURCE, ACCOUNT, client=client, use_hook_send=True)
            drv = WeChatRootDriver(
                edge,
                send_enabled=True,
                allowed_chats=[CHAT],
                ledger_path=ledger_path,
                truth_path=truth_path,
            )

            class RawMsg:
                def __init__(self, components):
                    self.components = components

            class MockGroup:
                group_id = CHAT

            class MockMsgInfo:
                group_info = MockGroup()
                user_info = None
                additional_config = None

            class MockMsg:
                message_id = "reply_out_001"
                message_info = MockMsgInfo()
                raw_message = RawMsg([
                    ReplyComponent(
                        target_message_id="#wechat:scope:" + ACCOUNT + ":143229",
                    ),
                    TextComponent("怪不得哦"),
                ])

            receipt = await drv.send_message(MockMsg(), route_key=drv.descriptor.route_key)
            assert receipt.status.name == "SENT"
            assert receipt.external_message_id == "143230"
            assert len(calls) == 1
            method, path, body = calls[0]
            assert path == "/wechat/send_quote"
            assert body["text"] == "怪不得哦"
            assert body["quote"]["server_id"] == "9138388385056250904"
            assert body["quote"]["talker"] == CHAT
            assert body["quote"]["sender_id"] == "wxid_friend"
            assert body["quote"]["display_name"] == "FriendRemark"
            assert body["quote"]["content"] == "上次不是我吗"

    asyncio.run(run())






