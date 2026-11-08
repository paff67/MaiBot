"""只有当前会话中明确出现的原文件路径可用于自动绑定。"""

from typing import Any, Dict, List, Sequence

from .wechat_screen_parser import WeChatBubble


def correlate_media_batch(
    bubbles: Sequence[WeChatBubble], items: Sequence[Dict[str, Any]],
    reference_paths: Sequence[str] = (),
) -> List[Dict[str, Any]]:
    allowed = set()
    for bubble in bubbles:
        if bubble.role == "OTHER":
            allowed.update({"image": {"image", "emoji"}, "voice": {"voice"}, "file": {"document", "video"}}.get(bubble.msg_type, set()))
    roots = {"image": "image2", "emoji": "emoji", "voice": "voice2", "video": "video"}
    selected = []
    for item in items:
        kind = item["kind"]
        if kind not in allowed:
            continue
        if kind == "document":
            suffix = f"/Android/data/com.tencent.mm/MicroMsg/Download/{item['relative_path']}"
        elif kind in roots:
            suffix = f"/com.tencent.mm/MicroMsg/{item['account']}/{roots[kind]}/{item['relative_path']}"
        else:
            continue
        if any(path.endswith(suffix) for path in reference_paths):
            selected.append(item)
    return selected
