"""从原始消息组件读取类型和文字长度，避免把识图描述当作用户文字。"""

from typing import Tuple

from src.chat.message_receive.message import SessionMessage
from src.common.data_models.message_component_data_model import TextComponent

_WECHAT_BARE_IMAGE_PLACEHOLDERS = frozenset({"[图片]"})
_WECHAT_BARE_EMOJI_PLACEHOLDERS = frozenset({"[表情包]"})
_WECHAT_BARE_MEDIA_PLACEHOLDERS = frozenset({"[语音]", "[视频]", "[文件]"})


def get_message_shape(message: SessionMessage) -> Tuple[int, int]:
    components = message.raw_message.components
    names = {component.format_name for component in components}
    text = "".join(component.text for component in components if isinstance(component, TextComponent)).strip()
    kind = (
        1
        if "image" in names
        else 2
        if "emoji" in names
        else 3
        if names & {"voice", "forward", "forward_node", "file", "video"}
        else 0
    )
    if kind == 0:
        msg_info = getattr(message, "message_info", None)
        additional_config = getattr(msg_info, "additional_config", None) or {}
        is_wechat_vault_media = additional_config.get("wechat_media_state") == "resolve_from_vault"
        is_wechat = str(getattr(message, "platform", "") or "").strip().lower() == "wechat"
        if is_wechat_vault_media or is_wechat:
            raw_msg_type = additional_config.get("wechat_msg_type")
            try:
                wechat_msg_type = int(raw_msg_type) if raw_msg_type is not None else None
            except (TypeError, ValueError):
                wechat_msg_type = None
            if wechat_msg_type == 3 or (is_wechat_vault_media and text in _WECHAT_BARE_IMAGE_PLACEHOLDERS) or (
                is_wechat and text in _WECHAT_BARE_IMAGE_PLACEHOLDERS
            ):
                return 1, 0
            if wechat_msg_type == 47 or (is_wechat_vault_media and text in _WECHAT_BARE_EMOJI_PLACEHOLDERS) or (
                is_wechat and text in _WECHAT_BARE_EMOJI_PLACEHOLDERS
            ):
                return 2, 0
            if wechat_msg_type in {34, 43} or (
                is_wechat_vault_media
                and (
                    text in _WECHAT_BARE_MEDIA_PLACEHOLDERS
                    or (text.startswith("{") and '"type"' in text)
                )
            ):
                return 3, 0
    return kind, len(text)
