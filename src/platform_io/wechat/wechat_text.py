"""微信出站短消息的 Markdown 与思考标签净化。"""

import re


def format_for_wechat(text: str) -> str:
    """去除残留 think 标签与多余 Markdown 外壳，输出干净微信文本。"""
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"```[\s\S]*?```", lambda match: re.sub(r"```\w*\n?", "", match.group(0)), cleaned)
    cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
    cleaned = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", cleaned)
    cleaned = re.sub(r"^#{1,6}\s+", "", cleaned, flags=re.MULTILINE)
    return cleaned.strip()
