"""Declarative channel projection shared by tool discovery and execution."""

from typing import Mapping

WECHAT_CAPABILITIES = {
    "send_text": True,
    "native_quote": True,
    "text_mention_fallback": True,
    "send_image": False,
    "send_emoji": False,
    "native_at": False,
    "read_forward_text": False,
    "phone_action": False,
}
TOOL_CAPABILITIES = {
    "send_image": "send_image",
    "send_emoji": "send_emoji",
    "show_emoji_list": "send_emoji",
    "view_forward_message": "read_forward_text",
    "phone_action": "phone_action",
    "reply": "send_text",
}
REPLY_CAPABILITIES = {"attach_pic": "send_image", "attach_emoji": "send_emoji", "attach_at": "mention"}


def channel_capabilities(
    platform: str, session_id: str = "", overrides: Mapping[str, bool] | None = None
) -> dict[str, bool]:
    caps = dict(WECHAT_CAPABILITIES) if platform == "wechat" else dict.fromkeys(WECHAT_CAPABILITIES, True)
    if platform == "wechat" and session_id:
        from src.chat.message_receive.chat_manager import chat_manager
        from src.platform_io.manager import get_platform_io_manager
        from src.platform_io.types import RouteKey

        stream = chat_manager.get_session_by_session_id(session_id)
        if stream is not None:
            drivers = get_platform_io_manager().resolve_drivers(
                RouteKey(platform, getattr(stream, "account_id", None), getattr(stream, "scope", None))
            )
            if drivers:
                caps.update(getattr(drivers[0], "channel_capabilities", {}))
    if overrides:
        caps.update({key: bool(value) for key, value in overrides.items() if key in caps})
    caps["mention"] = caps["native_at"] or caps["text_mention_fallback"]
    return caps


def unavailable_reply_arguments(arguments: dict, caps: Mapping[str, bool]) -> list[str]:
    return [name for name, capability in REPLY_CAPABILITIES.items() if arguments.get(name) and not caps[capability]]
