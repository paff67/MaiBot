"""WeChat driver for the existing Platform IO broker; native runtime owns reasoning."""

from pathlib import Path
from typing import Any, Dict, Optional
import asyncio
import hashlib
import logging
import os
import time

from src.companion_vault.group_members import sync_chat_directory_async
from src.platform_io.drivers.base import PlatformIODriver
from src.platform_io.types import (
    DeliveryReceipt,
    DeliveryStatus,
    DriverDescriptor,
    DriverKind,
    InboundMessageEnvelope,
    RouteKey,
)

from .edge_client import WeChatEdgeClient
from .ledger_source import LedgerSource
from .projection import enrich_record_from_truth_db, project_message

logger = logging.getLogger(__name__)

_SYSTEM_WECHAT_TALKERS = frozenset(
    {
        "weixin",
        "notifymessage",
        "notification_messages",
        "fmessage",
        "tmessage",
        "qmessage",
        "qqmail",
        "floatbottle",
        "medianote",
        "newsapp",
        "brandservice",
        "officialaccounts",
        "helper_entry",
        "voipapp",
        "voiceinputapp",
        "linkedinplugin",
        "shakeapp",
        "lbsapp",
        "feedsapp",
        "masssendapp",
        "meishiapp",
        "blogapp",
        "facebookapp",
        "qqfriend",
        "readerapp",
        "infoapp",
        "filehelper",
    }
)


def is_supported_wechat_chat_id(chat_id: Optional[str]) -> bool:
    """Return True for normal human contacts and @chatroom groups; False for WeChat system/official accounts."""
    chat = (chat_id or "").strip()
    if not chat or chat == "*":
        return False
    lower = chat.lower()
    if lower in _SYSTEM_WECHAT_TALKERS or lower.startswith("gh_"):
        return False
    return True


class WeChatRootDriver(PlatformIODriver):
    def __init__(
        self,
        edge: WeChatEdgeClient,
        *,
        allowed_chats=None,
        send_enabled: bool = False,
        ledger_path: Optional[str] = None,
        truth_path: Optional[str] = None,
        poll_interval: float = 1.0,
        backfill: bool = False,
    ):
        scope = hashlib.sha256(edge.source.encode()).hexdigest()[:24]
        super().__init__(
            DriverDescriptor(
                driver_id="wechat.root." + edge.account + "." + scope,
                kind=DriverKind.LOCAL,
                platform="wechat",
                account_id=edge.account,
                scope=scope,
            )
        )
        self.edge = edge
        self.allowed_chats = frozenset(["*"] if allowed_chats is None else allowed_chats)
        self.send_enabled = send_enabled
        self.ledger_path = ledger_path
        self.truth_path = truth_path
        self.poll_interval = max(0.05, float(poll_interval))
        self.backfill = backfill
        self.ledger_source = (
            LedgerSource(ledger_path, edge.source, edge.account, backfill=backfill)
            if ledger_path
            else None
        )
        self._poll_task: Optional[asyncio.Task[None]] = None
        self._directory_sync_ts: Dict[str, float] = {}
        self._group_names: Dict[str, str] = {}

    def _is_chat_allowed(self, chat_id: Optional[str]) -> bool:
        if not chat_id:
            return False
        if chat_id in self.allowed_chats:
            return True
        if "*" in self.allowed_chats:
            return is_supported_wechat_chat_id(chat_id)
        return False

    async def _poll_ledger_loop(self) -> None:
        assert self.ledger_source is not None
        while True:
            try:
                await self.ledger_source.consume_once(self)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("WeChat ledger consumer poll error: %s", type(exc).__name__)
            await asyncio.sleep(self.poll_interval)

    async def start(self) -> None:
        if self.ledger_source is not None and (self._poll_task is None or self._poll_task.done()):
            self._poll_task = asyncio.create_task(
                self._poll_ledger_loop(), name=f"wechat_ledger_poll:{self.driver_id}"
            )

    async def stop(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        await self.edge.close()

    async def deliver_committed(self, record: Dict[str, Any]) -> bool:
        if record["source"].rstrip("/") != self.edge.source or record["account"] != self.edge.account:
            raise ValueError("Wrong device/account")
        chat_id = str(record.get("chat_id") or "")
        if not self._is_chat_allowed(chat_id):
            return False
        if self.truth_path:
            now_mono = time.monotonic()
            if now_mono - self._directory_sync_ts.get(chat_id, 0.0) >= 60.0:
                self._directory_sync_ts[chat_id] = now_mono
                g_name = await sync_chat_directory_async(
                    self.truth_path,
                    self.edge.client,
                    source=self.edge.source,
                    account=self.edge.account,
                    chat_id=chat_id,
                )
                if g_name:
                    self._group_names[chat_id] = g_name
            if chat_id in self._group_names and not record.get("group_name"):
                record = dict(record, group_name=self._group_names[chat_id])
        if self.truth_path or self.ledger_path:
            record = await asyncio.to_thread(
                enrich_record_from_truth_db, record, self.truth_path, self.ledger_path
            )
        payload = project_message(record)
        return await self.emit_inbound(
            InboundMessageEnvelope(
                route_key=self.descriptor.route_key,
                driver_id=self.driver_id,
                driver_kind=self.descriptor.kind,
                external_message_id=payload["message_id"],
                payload=payload,
                metadata={"wechat_revision": record["revision"]},
            )
        )

    async def send_message(
        self, message, route_key: RouteKey, metadata: Optional[Dict[str, Any]] = None
    ) -> DeliveryReceipt:
        from src.mobile_agent.wechat_text import format_for_wechat

        try:
            from src.common.data_models.message_component_data_model import ReplyComponent, TextComponent
        except ModuleNotFoundError:
            ReplyComponent = TextComponent = ()  # type: ignore[assignment]

        def _is_text(c: Any) -> bool:
            return (bool(TextComponent) and isinstance(c, TextComponent)) or (
                getattr(c, "format_name", None) == "text" and isinstance(getattr(c, "text", None), str)
            )

        def _is_reply(c: Any) -> bool:
            return (bool(ReplyComponent) and isinstance(c, ReplyComponent)) or (
                getattr(c, "format_name", None) == "reply"
            )

        receipt = dict(
            internal_message_id=message.message_id,
            route_key=route_key,
            driver_id=self.driver_id,
            driver_kind=self.descriptor.kind,
        )
        group = getattr(message.message_info, "group_info", None)
        additional_config = getattr(message.message_info, "additional_config", None) or {}
        user_info = getattr(message.message_info, "user_info", None)
        chat = (
            group.group_id
            if (group and getattr(group, "group_id", None))
            else (
                additional_config.get("platform_io_target_group_id")
                or additional_config.get("platform_io_target_user_id")
                or (getattr(user_info, "user_id", None) if user_info else None)
            )
        )
        if (
            not self.send_enabled
            or route_key != self.descriptor.route_key
            or not self._is_chat_allowed(chat)
        ):
            return DeliveryReceipt(
                **receipt, status=DeliveryStatus.FAILED, error="WeChat send disabled or route not authorized"
            )
        components = message.raw_message.components if message.raw_message else []
        if not components or any(not (_is_text(c) or _is_reply(c)) for c in components):
            return DeliveryReceipt(
                **receipt,
                status=DeliveryStatus.FAILED,
                error="Only text transport verified; never silently discard attachments/replies",
            )
        raw_text = "".join(c.text for c in components if _is_text(c))
        text = format_for_wechat(raw_text)
        if not text:
            return DeliveryReceipt(
                **receipt,
                status=DeliveryStatus.FAILED,
                error="Only text transport verified; never silently discard attachments/replies",
            )
        try:
            result = await self.edge.send_text(chat, text, message.message_id)
        except ValueError as exc:
            return DeliveryReceipt(**receipt, status=DeliveryStatus.FAILED, error=str(exc))
        return DeliveryReceipt(
            **receipt,
            status=DeliveryStatus.SENT if result["status"] == "confirmed" else DeliveryStatus.PENDING,
            external_message_id=str(result["msg_id"]) if result.get("msg_id") else None,
            metadata=result,
        )


def configured_wechat_drivers(config_path: str) -> list[WeChatRootDriver]:
    import tomllib

    with Path(config_path).open("rb") as stream:
        config = tomllib.load(stream)
    if config.get("version") != 1:
        raise ValueError("Unsupported WeChat driver configuration")
    default_ledger = config.get("ledger_path", "data/message_ledger.db")
    default_truth = config.get("truth_path", "data/companion_truth.db")
    drivers: list[WeChatRootDriver] = []
    seen = set()
    for item in config.get("drivers", []):
        if not item.get("enabled", False):
            continue
        source = str(item["source"]).rstrip("/")
        account = str(item["account"])
        key = (source, account)
        if key in seen:
            raise ValueError("Duplicate WeChat driver scope")
        seen.add(key)
        token = item.get("token") or os.environ.get("MAIBOT_EDGE_TOKEN", "")
        edge = WeChatEdgeClient(
            source,
            account,
            token=token,
            use_hook_send=bool(item.get("use_hook_send", True)),
            max_sends_per_minute=int(item.get("max_sends_per_minute", 15)),
            max_sends_per_chat_per_minute=item.get("max_sends_per_chat_per_minute"),
            rate_limit_wait_seconds=float(item.get("rate_limit_wait_seconds", 0.0)),
        )
        raw_allowed = item.get("allowed_chats")
        driver = WeChatRootDriver(
            edge,
            allowed_chats=raw_allowed if raw_allowed else ["*"],
            send_enabled=bool(item.get("send_enabled", False)),
            ledger_path=item.get("ledger_path", default_ledger),
            truth_path=item.get("truth_path", default_truth),
            poll_interval=float(item.get("poll_interval", 1.0)),
            backfill=bool(item.get("backfill", False)),
        )
        drivers.append(driver)
    return drivers


async def register_wechat_driver(manager, driver: WeChatRootDriver):
    from src.platform_io.types import RouteBinding

    await manager.add_driver(driver)
    binding = RouteBinding(
        route_key=driver.descriptor.route_key, driver_id=driver.driver_id, driver_kind=driver.descriptor.kind
    )
    manager.bind_receive_route(binding)
    manager.bind_send_route(binding)
