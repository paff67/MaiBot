"""从 Root dumpsys 的 Chat_User 提取会话身份；缺失或多值时拒绝猜测。"""

from typing import Dict, List
import re


def parse_chat_identity(activity_dump: str) -> Dict[str, object]:
    values = set(re.findall(r"\bChat_User=([^,}\]\s]+)", activity_dump))
    if len(values) != 1:
        raise ValueError("Chat_User 缺失或存在多个会话，无法安全确定当前会话")
    chat_id = values.pop()
    if re.fullmatch(r"[A-Za-z0-9_.@-]{1,200}", chat_id) is None:
        raise ValueError("Chat_User 格式异常")
    return {"chat_id": chat_id, "is_group_chat": chat_id.endswith("@chatroom"),
            "identity_source": "android_activity_Chat_User"}


def extract_media_references(activity_dump: str, account: str) -> List[str]:
    """仅提取实际出现的当前账号原文件路径，不生成/推测路径。"""
    if re.fullmatch(r"[a-fA-F0-9]{32}", account) is None:
        raise ValueError("微信账号目录格式异常")
    root = rf"/data/(?:data|user/0)/com\.tencent\.mm/MicroMsg/{re.escape(account)}/"
    pattern = root + r"(?:image2|emoji|voice2|video)/[A-Za-z0-9_./-]+"
    downloads = r"/(?:sdcard|storage/emulated/0)/Android/data/com\.tencent\.mm/MicroMsg/Download/[^\s,}\]\"<>]+"
    return sorted(set(re.findall(pattern, activity_dump) + re.findall(downloads, activity_dump)))
