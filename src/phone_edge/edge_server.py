"""Android Root 环境下的标准库 HTTP 边缘服务。"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import FrameType
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlsplit
import hmac
import hashlib
import gzip
import json
import logging
import os
import signal
import subprocess
import threading
import time
import re
import xml.etree.ElementTree as ET

from .uinput_driver import UInputTouchDriver
from .media_store import MediaStore
from .chat_identity import extract_media_references, parse_chat_identity
from .send_ledger import RateLimitExceededError, SendLedger, compute_typing_delay_ms
from .notification_queue import read_notifications
from .message_journal import enqueue_send_spool, read_group_members, read_message_events, read_send_outbox
from .message_media import MessageMediaResolver
from .watcher_daemon import (
    find_active_wechat_account_dir,
    find_latest_wechat_image,
    find_latest_wechat_voice,
    transcode_silk_to_ogg,
)


def _android_system_env() -> Dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key != "LD_LIBRARY_PATH"}
    env["PATH"] = "/system/bin:/system/xbin"
    return env


class EdgeHTTPServer(ThreadingHTTPServer):
    # 退出时等请求线程结束，再销毁触控设备，避免正在执行的手势使用已关闭的 fd。
    daemon_threads = False

    def __init__(self, server_address: Tuple[str, int] = ("0.0.0.0", 8765)) -> None:
        self.driver: Optional[UInputTouchDriver] = None
        self.touch_lock = threading.Lock()
        self.xml_lock = threading.Lock()
        self.input_lock = threading.Lock()
        self.voice_lock = threading.Lock()
        self.media_store = MediaStore()
        self.message_media = MessageMediaResolver(self.media_store)
        self.media_token = os.environ.get("MAIBOT_EDGE_TOKEN", "")
        self.send_ledger = SendLedger("/data/adb/maibot_edge/run/send_requests.db")
        self.max_sends_per_minute = int(os.environ.get("MAIBOT_MAX_SENDS_PER_MINUTE", "15"))
        raw_allowed = os.environ.get("MAIBOT_ALLOWED_SEND_CHATS", "*")
        self.allowed_send_chats = {c.strip() for c in raw_allowed.split(",") if c.strip()}
        super().__init__(server_address, EdgeRequestHandler)

    def touch(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        # 初始化和整段手势共用一把锁，单指事件不能在并发请求间交错。
        with self.touch_lock:
            if self.driver is None:
                self.driver = UInputTouchDriver(1080, 2340)
            if path == "/touch/tap":
                point = self.driver.tap(**payload)
            elif path == "/touch/long_press":
                point = self.driver.long_press(**payload)
            else:
                self.driver.bezier_swipe(**payload)
                return {"ok": True}
            return {"ok": True, "point": list(point)}

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            with self.touch_lock:
                if self.driver is not None:
                    self.driver.close()


class EdgeRequestHandler(BaseHTTPRequestHandler):
    server: EdgeHTTPServer

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        compressed = content_type.startswith("application/json") and len(body)>16384 and "gzip" in self.headers.get("Accept-Encoding", "")
        if compressed:
            body = gzip.compress(body, compresslevel=1)
        self.send_response(status)
        if compressed:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Dict[str, Any], status: int = 200) -> None:
        self._send(json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", status)

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            raise ValueError("请求体必须是非空 JSON 对象")
        payload = json.loads(self.rfile.read(length))
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return payload

    def do_GET(self) -> None:
        try:
            if not self._authorize_media():
                return
            self._get(urlsplit(self.path).path)
        except Exception as exc:
            logging.exception("GET %s 失败", self.path)
            self._json({"ok": False, "error": str(exc)}, 500)

    def _authorize_media(self) -> bool:
        token = self.server.media_token
        if token and not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
            self._json({"ok": False, "error": "Unauthorized"}, 401)
            return False
        return True

    def _get(self, path: str) -> None:
        if path == '/wechat/group_members':
            query=parse_qs(urlsplit(self.path).query)
            self._json(read_group_members(query['account'][0],query['group_id'][0]))
            return
        if path == '/messages/media_resolve':
            query=parse_qs(urlsplit(self.path).query)
            self._json(self.server.message_media.resolve(query['account'][0],query['msg_id'][0],
                query['revision'][0],int(query.get('ref_index',['0'])[0])))
        elif path == "/messages/events":
            query = parse_qs(urlsplit(self.path).query)
            self._json(read_message_events(int(query.get("after", ["0"])[0]), int(query.get("limit", ["100"])[0])))
        elif path == "/notifications/events":
            query = parse_qs(urlsplit(self.path).query)
            self._json(read_notifications("/data/adb/maibot_edge/state/notifications.db",
                int(query.get("after", ["0"])[0]), int(query.get("limit", ["100"])[0])))
        elif path in {"/wechat/chat_identity", "/wechat/chat_snapshot"}:
            with self.server.xml_lock:
                def identify():
                    result = subprocess.run(["/system/bin/dumpsys", "activity", "com.tencent.mm"],
                                            check=True, capture_output=True, text=True,
                                            env=_android_system_env(), timeout=20)
                    identity = parse_chat_identity(result.stdout)
                    identity["account"] = find_active_wechat_account_dir().name
                    return identity, result.stdout
                before, _ = identify()
                if path == "/wechat/chat_snapshot":
                    subprocess.run(["/system/bin/uiautomator", "dump", "/data/local/tmp/edge_ui.xml"],
                                   check=True, capture_output=True, env=_android_system_env(), timeout=30)
                    xml = Path("/data/local/tmp/edge_ui.xml").read_text(encoding="utf-8")
                    packages = {node.get("package") for node in ET.fromstring(xml).iter()
                                if node.get("package") and node.get("package") not in {"com.android.systemui"}}
                    if "com.tencent.mm" not in packages:
                        raise ValueError("微信不在当前可见界面，拒绝使用后台残留 Chat_User")
                    after, activity_dump = identify()
                    if before != after:
                        raise ValueError("采集期间会话切换，丢弃本次快照")
                    before["xml"] = xml
                    before["media_reference_paths"] = extract_media_references(activity_dump, before["account"])
                before["captured_at_ns"] = time.time_ns()
                before["protocol_version"] = 2
                self._json({"ok": True, **before})
        elif path == "/wechat/media_since":
            query = parse_qs(urlsplit(self.path).query)
            self._json(self.server.media_store.scan(
                since_ns=int(query.get("mtime_ns", ["0"])[0]),
                after=query.get("after", [""])[0], limit=int(query.get("limit", ["100"])[0]),
                until_ns=int(query.get("until_ns", ["0"])[0]),
            ))
        elif path in {"/wechat/media_file", "/wechat/media_ogg", "/wechat/media_wav"}:
            query = parse_qs(urlsplit(self.path).query)
            data, digest = self.server.media_store.read(
                account=query["account"][0], kind=query["kind"][0], relative_path=query["path"][0],
                mtime_ns=int(query["mtime_ns"][0]), ctime_ns=int(query.get("ctime_ns", ["0"])[0]),
                size=int(query["size"][0]),
            )
            if path in {"/wechat/media_ogg", "/wechat/media_wav"}:
                with self.server.voice_lock:
                    data = (self.server.media_store.voice_wav(data) if path.endswith("_wav")
                            else self.server.media_store.voice_ogg(data))
                digest = hashlib.sha256(data).hexdigest()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-SHA256", digest)
            self.end_headers()
            self.wfile.write(data)
        elif path == "/health":
            self._json({"ok": True, "resolution": [1080, 2340]})
        elif path == "/screen/xml":
            # dump 使用固定文件名，读文件也必须包含在锁内。
            with self.server.xml_lock:
                subprocess.run(
                    ["/system/bin/uiautomator", "dump", "/data/local/tmp/edge_ui.xml"],
                    check=True,
                    env=_android_system_env(),
                )
                body = Path("/data/local/tmp/edge_ui.xml").read_bytes()
            self._send(body, "application/xml; charset=utf-8")
        elif path == "/screen/screenshot":
            body = subprocess.run(
                ["/system/bin/screencap", "-p"],
                check=True,
                stdout=subprocess.PIPE,
                env=_android_system_env(),
            ).stdout
            self._send(body, "image/png")
        elif path == "/wechat/active_dir":
            self._json({"ok": True, "path": str(find_active_wechat_account_dir())})
        elif path == "/wechat/latest_voice.ogg":
            # 转码使用固定临时文件，锁覆盖解码、转码和最终读取。
            with self.server.voice_lock:
                voice_path = find_latest_wechat_voice()
                body = transcode_silk_to_ogg(str(voice_path))
            self._send(body, "audio/ogg")
        elif path == "/wechat/latest_image":
            image = find_latest_wechat_image()
            if image is None:
                self._json({"ok": False, "error": "未找到合法微信图片"}, 404)
            else:
                image_path, mime_type = image
                self._send(image_path.read_bytes(), mime_type)
        else:
            self._json({"ok": False, "error": "接口不存在"}, 404)

    def do_POST(self) -> None:
        if not self._authorize_media():
            return
        path = urlsplit(self.path).path
        if path not in (
            "/touch/tap",
            "/touch/long_press",
            "/touch/swipe",
            "/touch/input_text",
            "/wechat/send_text",
            "/wechat/send_quote",
            "/backlight",
        ):
            self._json({"ok": False, "error": "接口不存在"}, 404)
            return
        try:
            payload = self._read_json()
            if path in ("/wechat/send_text", "/wechat/send_quote"):
                text = payload["text"]
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("text 必须是非空字符串")
                expected_account = payload.get("expected_account")
                expected_chat = payload.get("expected_chat_id")
                request_id = payload.get("request_id", "")
                quote = payload.get("quote")
                if not expected_account or not expected_chat:
                    raise ValueError("自动发送必须提供目标账号和 Chat_User")
                if (
                    self.server.allowed_send_chats
                    and "*" not in self.server.allowed_send_chats
                    and expected_chat not in self.server.allowed_send_chats
                ):
                    raise ValueError(f"目标会话 {expected_chat} 不在允许发送白名单内")
                if find_active_wechat_account_dir().name != expected_account:
                    raise ValueError("手机端微信活跃账号校验失败")
                digest_payload = {"text": text, "account": expected_account, "chat_id": expected_chat}
                if quote:
                    digest_payload["quote"] = quote
                with self.server.input_lock:
                    previous = self.server.send_ledger.lookup(request_id, digest_payload)
                    if previous is not None:
                        if previous.get("status") == "submitted_unconfirmed":
                            latest_outbox = read_send_outbox(request_id)
                            if latest_outbox is not None and latest_outbox.get("status") in {
                                "confirmed",
                                "failed",
                                "rate_limited",
                            }:
                                previous = dict(previous)
                                previous["status"] = latest_outbox["status"]
                                for key in (
                                    "msg_id",
                                    "msg_svr_id",
                                    "create_time",
                                    "send_layer",
                                    "chatting_context_talker",
                                ):
                                    if latest_outbox.get(key) is not None:
                                        previous[key] = latest_outbox[key]
                                self.server.send_ledger.update_submitted(request_id, previous)
                        self._json(previous)
                        return
                    max_per_min = int(payload.get("max_per_minute", self.server.max_sends_per_minute))
                    rate_info = self.server.send_ledger.check_and_acquire_rate_slot(
                        request_id, expected_chat, max_per_minute=max_per_min
                    )
                    self.server.send_ledger.reserve(request_id, digest_payload)
                    typing_delay_ms = compute_typing_delay_ms(
                        text,
                        base_min_ms=150,
                        base_max_ms=350,
                        per_char_min_ms=20,
                        per_char_max_ms=45,
                        jitter_std_ms=40.0,
                        min_delay_ms=int(payload.get("min_typing_delay_ms", 250)),
                        max_delay_ms=int(payload.get("max_typing_delay_ms", 1200)),
                    )
                    time.sleep(typing_delay_ms / 1000.0)
                    spool_allowed_chats = (
                        sorted(self.server.allowed_send_chats)
                        if (self.server.allowed_send_chats and "*" not in self.server.allowed_send_chats)
                        else [expected_chat]
                    )
                    spool_data = {
                        "request_id": request_id,
                        "account": expected_account,
                        "chat_id": expected_chat,
                        "content": text,
                        "max_per_minute": max_per_min,
                        "allowed_chats": spool_allowed_chats,
                    }
                    if quote:
                        spool_data["msg_type"] = "quote"
                        spool_data["quote"] = quote
                    enqueue_send_spool(spool_data)
                    timeout_s = float(payload.get("timeout_s", 18.0))
                    deadline = time.monotonic() + timeout_s
                    outbox = None
                    while time.monotonic() < deadline:
                        outbox = read_send_outbox(request_id)
                        if outbox is not None and outbox.get("status") not in {"sending", "accepted"}:
                            break
                        time.sleep(0.15)
                    if outbox is None or outbox.get("status") in {"sending", "accepted"}:
                        status = "submitted_unconfirmed"
                        outbox = outbox or {}
                    elif outbox.get("status") == "rate_limited":
                        raise RateLimitExceededError(
                            outbox.get("error") or "Frida 端单分钟发送频率超限",
                            count=max_per_min,
                            limit=max_per_min,
                            retry_after_ms=60000,
                        )
                    elif outbox.get("status") == "failed":
                        raise RuntimeError(outbox.get("error") or "Frida 内部发送失败")
                    else:
                        status = outbox["status"]
                    result = {
                        "ok": True,
                        "sent": True,
                        "status": status,
                        "request_id": request_id,
                        "account": expected_account,
                        "chat_id": expected_chat,
                        "text": text,
                        "msg_id": outbox.get("msg_id"),
                        "msg_svr_id": outbox.get("msg_svr_id"),
                        "create_time": outbox.get("create_time"),
                        "send_layer": outbox.get("send_layer"),
                        "chatting_context_talker": outbox.get("chatting_context_talker"),
                        "typing_delay_ms": typing_delay_ms,
                        "rate_limit": rate_info,
                    }
                    self.server.send_ledger.submitted(request_id, result)
            elif path == "/touch/input_text":
                text = payload["text"]
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("text 必须是非空字符串")
                tap_x, tap_y = payload.get("tap_x"), payload.get("tap_y")
                if (tap_x is None) != (tap_y is None):
                    raise ValueError("tap_x 和 tap_y 必须同时提供")
                if tap_x is not None and (type(tap_x) is not int or type(tap_y) is not int):
                    raise ValueError("tap_x 和 tap_y 必须是整数")
                auto_send = bool(payload.get("auto_send", False))
                send_x = int(payload.get("send_x", 984))
                send_y = int(payload.get("send_y", 1335))
                with self.server.input_lock:
                    if auto_send:
                        expected_account = payload.get("expected_account")
                        expected_chat = payload.get("expected_chat_id")
                        if not expected_account or not expected_chat:
                            raise ValueError("自动发送必须提供目标账号和 Chat_User")
                        activity = subprocess.run(
                            ["/system/bin/dumpsys", "activity", "com.tencent.mm"],
                            check=True, capture_output=True, text=True, env=_android_system_env(), timeout=20,
                        )
                        identity = parse_chat_identity(activity.stdout)
                        if identity["chat_id"] != expected_chat or find_active_wechat_account_dir().name != expected_account:
                            raise ValueError("手机端会话校验失败，拒绝输入和发送")
                        request_id = payload.get("request_id", "")
                        previous = self.server.send_ledger.reserve(request_id, {
                            "text": text, "account": expected_account, "chat_id": expected_chat,
                        })
                        if previous is not None:
                            self._json(previous)
                            return
                    if tap_x is not None and tap_y is not None:
                        self.server.touch("/touch/tap", {"x": int(tap_x), "y": int(tap_y)})
                        time.sleep(0.25)
                    # 文本只通过环境变量传入固定命令，shell 不会重新解释文本中的引号或替换表达式。
                    env = _android_system_env()
                    env["CLIP_TEXT"] = text
                    subprocess.run(
                        ["/system/bin/su", "2000", "-c",
                         'CLASSPATH=/data/adb/maibot_edge/bin/ClipSetter.dex '
                         '/system/bin/app_process /system/bin ClipSetter "$CLIP_TEXT"'],
                        check=True, env=env,
                    )
                    subprocess.run(
                        ["/system/bin/input", "keyevent", "279"],
                        check=True, env=_android_system_env(),
                    )
                    if auto_send:
                        # 键盘出现后输入栏位置会改变，发送坐标必须来自粘贴后的新 UI。
                        with self.server.xml_lock:
                            subprocess.run(["/system/bin/uiautomator", "dump", "/data/local/tmp/edge_send.xml"],
                                           check=True, capture_output=True, env=_android_system_env(), timeout=30)
                            tree = ET.fromstring(Path("/data/local/tmp/edge_send.xml").read_text(encoding="utf-8"))
                            controls = []
                            for node in tree.iter():
                                if node.get("text") not in {"发送", "Send"} and node.get("content-desc") not in {"发送", "Send"}:
                                    continue
                                bounds = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
                                if bounds:
                                    x1, y1, x2, y2 = map(int, bounds.groups())
                                    if x1 >= 800 and y1 >= 1000 and x2 > x1 and y2 > y1:
                                        controls.append(((x1 + x2) // 2, (y1 + y2) // 2))
                            if len(set(controls)) != 1:
                                raise ValueError("粘贴后无法唯一定位发送按钮，停止发送")
                            send_x, send_y = controls[0]
                            activity = subprocess.run(["/system/bin/dumpsys", "activity", "com.tencent.mm"],
                                check=True, capture_output=True, text=True, env=_android_system_env(), timeout=20)
                            if (parse_chat_identity(activity.stdout)["chat_id"] != expected_chat
                                    or find_active_wechat_account_dir().name != expected_account):
                                raise ValueError("粘贴后会话改变，拒绝点击发送")
                        self.server.touch("/touch/tap", {"x": send_x, "y": send_y})
                result = {"ok": True, "text": text, "sent": auto_send}
                if auto_send:
                    result["request_id"] = request_id
                    result["status"] = "submitted"
                    self.server.send_ledger.submitted(request_id, result)
            elif path == "/backlight":
                brightness = payload["brightness"]
                if type(brightness) is not int or brightness < 0:
                    raise ValueError("brightness 必须是非负整数")
                Path("/sys/class/backlight/panel0-backlight/brightness").write_text(
                    str(brightness), encoding="utf-8"
                )
                result = {"ok": True, "brightness": brightness}
            else:
                result = self.server.touch(path, payload)
        except RateLimitExceededError as exc:
            self._json(
                {
                    "ok": False,
                    "status": "rate_limited",
                    "error": str(exc),
                    "count": exc.count,
                    "limit": exc.limit,
                    "retry_after_ms": exc.retry_after_ms,
                },
                429,
            )
            return
        except (KeyError, TypeError, ValueError) as exc:
            self._json({"ok": False, "error": str(exc)}, 400)
            return
        except Exception as exc:
            logging.exception("POST %s 失败", self.path)
            self._json({"ok": False, "error": str(exc)}, 500)
            return
        self._json(result)


def _exit_on_signal(signum: int, frame: Optional[FrameType]) -> None:
    raise SystemExit(0)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    signal.signal(signal.SIGTERM, _exit_on_signal)
    signal.signal(signal.SIGINT, _exit_on_signal)
    with EdgeHTTPServer() as server:
        logging.info("边缘服务监听 %s:%s", *server.server_address)
        server.serve_forever()


if __name__ == "__main__":
    main()
