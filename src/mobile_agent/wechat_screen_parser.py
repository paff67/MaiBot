"""无需 VLM 的微信聊天窗几何解析及会话内增量检测。"""

from dataclasses import dataclass
from difflib import SequenceMatcher
from hashlib import sha256
from typing import Dict, List, Literal, Optional, Set, Tuple
from uuid import uuid4
import re
import xml.etree.ElementTree as ET

Role = Literal["OTHER", "SELF", "SYSTEM"]
MessageType = Literal["text", "voice", "image", "file", "card"]
Bounds = Tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class WeChatBubble:
    role: Role
    sender_name: str
    msg_type: MessageType
    content: str
    bounds: Bounds
    center: Tuple[int, int]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class WeChatScreenState:
    chat_title: str
    is_group_chat: bool
    bubbles: Tuple[WeChatBubble, ...]
    input_box_center: Optional[Tuple[int, int]]
    send_button_center: Optional[Tuple[int, int]]
    conversation_id: str = ""


@dataclass(frozen=True, slots=True)
class _Node:
    element: ET.Element
    text: str
    description: str
    bounds: Bounds

    @property
    def center(self) -> Tuple[int, int]:
        x1, y1, x2, y2 = self.bounds
        return (x1+x2)//2, (y1+y2)//2

    @property
    def content(self) -> str:
        return self.text if self.text else self.description


class WeChatScreenParser:
    def __init__(self, screen_width: int = 1080, screen_height: int = 2340,
                 include_latest_on_init: bool = True) -> None:
        self.screen_width = screen_width
        self.screen_height = screen_height
        self.include_latest_on_init = include_latest_on_init
        self._seen_fingerprints: Dict[str, Set[str]] = {}
        self._previous_screens: Dict[str, Tuple[WeChatBubble, ...]] = {}
        self._fingerprint_counters: Dict[str, int] = {}
        self._instance_id = uuid4().hex

    def _nodes(self, xml_text: str) -> List[_Node]:
        nodes = []
        for element in ET.fromstring(xml_text).iter():
            match = re.fullmatch(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", element.get("bounds", ""))
            if match is None:
                continue
            x1, y1, x2, y2 = map(int, match.groups())
            if x2 <= x1 or y2 <= y1 or x2 <= 0 or y2 <= 0 or x1 >= self.screen_width or y1 >= self.screen_height:
                continue
            nodes.append(_Node(element, element.get("text", "").strip(),
                               element.get("content-desc", "").strip(), (x1, y1, x2, y2)))
        return sorted(nodes, key=lambda node: (node.bounds[1], node.bounds[0]))

    def _avatar_role(self, node: _Node) -> Optional[Role]:
        x1, y1, x2, y2 = node.bounds
        if not (50 <= x2-x1 <= 165 and 0.75 <= (x2-x1)/(y2-y1) <= 1.3):
            return None
        if node.text or not ("头像" in node.description or "avatar" in node.description.lower()):
            return None
        if x1 < int(self.screen_width * 0.2):
            return "OTHER"
        if x2 > int(self.screen_width * 0.8):
            return "SELF"
        return None

    @staticmethod
    def _message_type(node: _Node) -> MessageType:
        if re.fullmatch(r'\d+["″”]', node.content) or "语音" in node.description:
            return "voice"
        if re.search(r"\.(pdf|docx|xlsx|pptx|txt|csv|zip|mp4)(?:\b|$)", node.content, re.IGNORECASE):
            return "file"
        if "图片" in node.description or not node.content:
            return "image"
        return "text"

    def parse_screen(
        self, xml_text: str, fallback_title: str = "", *, conversation_id: str = "",
        is_group_chat: Optional[bool] = None,
    ) -> WeChatScreenState:
        nodes = self._nodes(xml_text)
        # 微信顶部导航栏严格在 y <= 215 以内，RecyclerView 从 y = 217~220 开始；
        # 防止向上滚动的聊天气泡（y=217..255）被误识别为顶部群标题。
        top, bottom = int(self.screen_height * 0.092), int(self.screen_height * 0.87)
        titles = [node for node in nodes if node.bounds[3] <= top and node.bounds[0] >= int(self.screen_width * 0.22)
                  and node.bounds[2] <= int(self.screen_width * 0.82)
                  and node.text and node.text not in {"返回", "Back", "微信", "WeChat"}
                  and not re.fullmatch(r"\d{1,2}:\d{2}", node.text)]
        title = min(titles, key=lambda node: abs(node.center[0] - self.screen_width//2)).text if titles else fallback_title
        is_group = is_group_chat if is_group_chat is not None else (
            re.search(r"\(\d+\)$", title) is not None or title.endswith("@chatroom")
        )
        inputs = [node for node in nodes if node.bounds[1] >= int(self.screen_height * 0.45)
                  and node.element.get("class", "").endswith("EditText")]
        if inputs:
            bottom = inputs[0].bounds[1] - 10
        footer = [node for node in nodes if node.bounds[1] >= bottom]
        if not inputs:
            inputs = [node for node in footer if node.element.get("clickable") == "true"
                      and self.screen_width * 0.2 < node.center[0] < self.screen_width * 0.78
                      and node.bounds[2]-node.bounds[0] > self.screen_width * 0.25]
        sends = [node for node in footer if node.bounds[0] >= int(self.screen_width * 0.78)
                 and (node.text in {"发送", "Send"} or node.description in {"发送", "Send"})]
        middle = [node for node in nodes if top < node.bounds[1] < bottom]
        avatars = [(node, self._avatar_role(node)) for node in middle if self._avatar_role(node) is not None]
        avatar_elements = {node.element for node, _ in avatars}
        candidates = [node for node in middle if node.element not in avatar_elements
                      and (node.content or node.element.get("clickable") == "true"
                           or (node.element.get("class", "").endswith("ImageView")
                               and min(node.bounds[2]-node.bounds[0], node.bounds[3]-node.bounds[1]) > 165))]
        # 容器与文字子节点常重复表达同一气泡；保留有内容的后代，排除整行/列表容器。
        candidate_elements = {node.element for node in candidates}
        candidates = [node for node in candidates
                      if not any(child in candidate_elements or child in avatar_elements
                                 for child in list(node.element.iter())[1:])]
        assigned: List[Tuple[_Node, Optional[_Node], Role]] = []
        for node in candidates:
            x1, y1, x2, y2 = node.bounds
            matches = [(avatar, role) for avatar, role in avatars
                       if min(y2, avatar.bounds[3]) > max(y1, avatar.bounds[1])
                       and ((role == "OTHER" and x1 >= avatar.bounds[2] - 25)
                            or (role == "SELF" and x2 <= avatar.bounds[0] + 25))]
            if matches:
                avatar, role = min(matches, key=lambda item: abs(node.center[1]-item[0].center[1]))
                assigned.append((node, avatar, role))
            elif node.content and abs(node.center[0]-self.screen_width//2) < 120:
                assigned.append((node, None, "SYSTEM"))
        # 微信头像自带 desc="<昵称>头像"；头像旁与之同名的顶部文本行即为群成员昵称标签。
        names: Dict[ET.Element, str] = {}
        name_elements: Set[ET.Element] = set()
        for avatar, _ in avatars:
            if avatar.description:
                desc_name = re.sub(r"(?:的)?头像$|(?i:avatar)$", "", avatar.description).strip()
                if desc_name:
                    names[avatar.element] = desc_name
        for node, avatar, role in assigned:
            if avatar is None or role != "OTHER" or not node.text:
                continue
            avatar_name = names.get(avatar.element, "")
            if (avatar_name and node.text == avatar_name and node.bounds[1] <= avatar.bounds[1] + 25) or (
                is_group
                and node.bounds[3]-node.bounds[1] <= (avatar.bounds[3]-avatar.bounds[1]) * 0.45
                and any(other.bounds[1] >= node.bounds[3] and owner is avatar for other, owner, _ in assigned)
            ):
                names[avatar.element] = node.text
                name_elements.add(node.element)
                if is_group_chat is None:
                    is_group = True
        bubbles: List[WeChatBubble] = []
        voice_nodes: Dict[ET.Element, _Node] = {}
        for node, avatar, _ in assigned:
            if avatar is not None and node.element not in name_elements and self._message_type(node) == "voice":
                previous = voice_nodes.get(avatar.element)
                if previous is None or "语音" in node.description:
                    voice_nodes[avatar.element] = node
        for node, avatar, role in assigned:
            if node.element in name_elements:
                continue
            if avatar is not None and avatar.element in voice_nodes and node is not voice_nodes[avatar.element]:
                continue
            sender = ""
            if role == "SELF":
                sender = "我"
            elif avatar is not None:
                sender = names.get(avatar.element, "")
                if not sender and avatar.description:
                    sender = re.sub(r"(?:的)?头像$|(?i:avatar)$", "", avatar.description).strip()
                if not sender and not is_group:
                    sender = title
            kind = self._message_type(node)
            content = node.content
            # Fingerprints are assigned after sequence alignment. Content adjacency alone
            # collides for repeated identical bubbles and loses messages in a run.
            fingerprint = ""
            bubbles.append(WeChatBubble(role, sender, kind, content, node.bounds, node.center, fingerprint))
        stream_key = conversation_id or title
        old = self._previous_screens.get(stream_key, ())

        def key(bubble: WeChatBubble) -> Tuple[str, str, str, str]:
            return bubble.role, bubble.sender_name, bubble.msg_type, bubble.content

        matched_new: Set[int] = set()
        for block in SequenceMatcher(None, [key(item) for item in old],
                                     [key(item) for item in bubbles], autojunk=False).get_matching_blocks():
            for offset in range(block.size):
                old_index, new_index = block.a + offset, block.b + offset
                matched_new.add(new_index)
                bubble = bubbles[new_index]
                bubbles[new_index] = WeChatBubble(
                    bubble.role, bubble.sender_name, bubble.msg_type, bubble.content,
                    bubble.bounds, bubble.center, old[old_index].fingerprint,
                )
        for index, bubble in enumerate(bubbles):
            if index in matched_new:
                continue
            sequence = self._fingerprint_counters.get(stream_key, 0) + 1
            self._fingerprint_counters[stream_key] = sequence
            fingerprint = sha256(f"{self._instance_id}|{stream_key}|{sequence}|{key(bubble)}".encode()).hexdigest()[:16]
            bubbles[index] = WeChatBubble(
                bubble.role, bubble.sender_name, bubble.msg_type, bubble.content,
                bubble.bounds, bubble.center, fingerprint,
            )
        result = tuple(bubbles)
        self._previous_screens[stream_key] = result
        return WeChatScreenState(title, is_group, result, inputs[0].center if inputs else None,
                                 sends[0].center if sends else None, stream_key)

    def extract_new_incoming_bubbles(self, state: WeChatScreenState) -> List[WeChatBubble]:
        seen = self._seen_fingerprints.setdefault(state.conversation_id or state.chat_title, set())
        if not seen:
            seen.update(bubble.fingerprint for bubble in state.bubbles)
            if self.include_latest_on_init and state.bubbles and state.bubbles[-1].role == "OTHER":
                return [state.bubbles[-1]]
            return []
        incoming = []
        for bubble in state.bubbles:
            if bubble.fingerprint not in seen and bubble.role == "OTHER":
                incoming.append(bubble)
            seen.add(bubble.fingerprint)
        return incoming
