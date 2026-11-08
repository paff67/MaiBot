"""ARTEMIS UI 树和 Set-of-Mark 截图双通道载荷。"""

from io import BytesIO
from typing import Any, Dict, List, Optional, Sequence, Tuple
from PIL import Image, ImageDraw
import re
import xml.etree.ElementTree as ET


def parse_and_prune_ui_tree(xml_text: str, screen_width: int = 1080, screen_height: int = 2340) -> List[Dict[str, Any]]:
    elements: List[Dict[str, Any]] = []
    for node in ET.fromstring(xml_text).iter():
        match = re.fullmatch(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", node.get("bounds", ""))
        if match is None:
            continue
        x1, y1, x2, y2 = map(int, match.groups())
        if x2-x1 <= 5 or y2-y1 <= 5 or x2 <= 0 or y2 <= 0 or x1 >= screen_width or y1 >= screen_height:
            continue
        text, description = node.get("text", "").strip(), node.get("content-desc", "").strip()
        class_name = node.get("class", "")
        actions = []
        for attribute, action in (("clickable", "tap"), ("long-clickable", "long_press")):
            if node.get(attribute) == "true":
                actions.append(action)
        if class_name.endswith("EditText"):
            actions.append("input_text")
        if node.get("scrollable") == "true":
            actions.append("swipe")
        if text or description or actions:
            elements.append({"text": text, "content_desc": description, "class": class_name,
                             "bounds": [x1, y1, x2, y2], "center": [(x1+x2)//2, (y1+y2)//2],
                             "actions": actions})
    elements.sort(key=lambda el: (el["bounds"][1], el["bounds"][0]))
    for index, element in enumerate(elements, 1):
        element["id"] = index
    return elements


def render_som_screenshot(image_bytes: bytes, elements: Sequence[Dict[str, Any]]) -> bytes:
    with Image.open(BytesIO(image_bytes)) as source:
        image = source.convert("RGB")
    draw = ImageDraw.Draw(image)
    for element in elements:
        x1, y1, x2, y2 = element["bounds"]
        draw.rectangle((x1, y1, x2, y2), outline="#FF1744", width=3)
        label = f"#{element['id']}"
        left, top, right, bottom = draw.textbbox((0, 0), label)
        label_y = max(0, y1 - 22)
        draw.rectangle((x1, label_y, x1 + right-left+8, label_y + 21), fill="#FF1744")
        draw.text((x1+4-left, label_y+3-top), label, fill="white")
    output = BytesIO()
    image.save(output, format="JPEG")
    return output.getvalue()


def build_artemis_mcp_payload(
    xml_text: str, screenshot_bytes: Optional[bytes] = None, package_name: str = "",
    activity_name: str = "", resolution: Tuple[int, int] = (1080, 2340),
) -> Dict[str, Any]:
    elements = parse_and_prune_ui_tree(xml_text, *resolution)
    return {"screen_meta": {"package": package_name, "activity": activity_name, "resolution": list(resolution)},
            "interactive_elements": elements,
            "som_screenshot_bytes": None if screenshot_bytes is None else render_som_screenshot(screenshot_bytes, elements)}
