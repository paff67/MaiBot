"""微信短消息的 Markdown/思考标签净化。"""

import re


def format_for_wechat(text: str) -> str:
    """参考旧桥接行为去掉 think 标签和 Markdown 外壳。"""
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"```[\s\S]*?```", lambda match: re.sub(r"```\w*\n?", "", match.group(0)), cleaned)
    cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
    cleaned = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", cleaned)
    cleaned = re.sub(r"^#{1,6}\s+", "", cleaned, flags=re.MULTILINE)
    return cleaned.strip()
