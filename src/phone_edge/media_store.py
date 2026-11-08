"""Root 文件系统媒体清单；不把文件时间误当成发送者或会话身份。"""

from pathlib import Path
from typing import Any, Dict, List, Tuple
import hashlib
import io
import mimetypes
import os
import re
import stat
import subprocess
import tempfile
import time
import wave

from .watcher_daemon import find_active_wechat_account_dir


class MediaStore:
    def __init__(self, micromsg_root: str = "/data/data/com.tencent.mm/MicroMsg",
                 download_root: str = "/sdcard/Android/data/com.tencent.mm/MicroMsg/Download",
                 settle_seconds: float = 0.8) -> None:
        self.micromsg_root = micromsg_root
        self.download_root = Path(download_root).resolve()
        self.settle_ns = int(settle_seconds * 1_000_000_000)

    def roots(self, account_id: str = '') -> Dict[str, Path]:
        if account_id:
            if re.fullmatch(r'[a-f0-9]{32}', account_id) is None:
                raise ValueError('Invalid account directory')
            account = Path(self.micromsg_root) / account_id
            if not account.is_dir() or account.is_symlink():
                raise ValueError('Account directory unavailable')
        else:
            account = find_active_wechat_account_dir(self.micromsg_root)
        return {"image": (account / "image2").resolve(),
                "emoji": (account / "emoji").resolve(),
                "voice": (account / "voice2").resolve(), "video": (account / "video").resolve(),
                "document": self.download_root, "attachment": (account / 'attachment').resolve(),
                "opendata": (account / 'message/media/opendata').resolve()}

    @staticmethod
    def describe(path: Path, kind: str) -> Tuple[str, str]:
        with path.open("rb") as source:
            magic = source.read(16)
        if magic.startswith(b"\xff\xd8\xff"):
            return "image/jpeg", "original"
        if magic.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png", "original"
        if magic.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif", "original"
        if magic[:4] == b"RIFF" and magic[8:12] == b"WEBP":
            return "image/webp", "original"
        if magic.startswith((b"\x02#!SILK_V3", b"#!SILK_V3")):
            return "audio/x-silk", "conversion_required"
        if magic.startswith(b"%PDF-"):
            return "application/pdf", "original"
        if magic[4:8] == b"ftyp":
            return "video/mp4", "original"
        if magic[:4].lower() == b"wxgf" or kind == "image":
            return "application/octet-stream", "opaque_original"
        return mimetypes.guess_type(path.name)[0] or "application/octet-stream", "original"

    def scan(self, since_ns: int = 0, after: str = "", limit: int = 100,
             until_ns: int = 0) -> Dict[str, Any]:
        if since_ns < 0 or not 1 <= limit <= 500:
            raise ValueError("无效游标或分页大小")
        cutoff = time.time_ns() - self.settle_ns
        until_ns = min(until_ns, cutoff) if until_ns else cutoff
        roots = self.roots()
        account = roots["image"].parent.name
        entries: List[Dict[str, Any]] = []
        for kind, root in roots.items():
            if not root.exists():
                continue
            # 不跟随目录或文件符号链接，不提供任意 Root 路径下载接口。
            for directory, dirs, files in os.walk(root, followlinks=False):
                dirs[:] = [d for d in dirs if not Path(directory, d).is_symlink()]
                for name in files:
                    path = Path(directory, name)
                    info = path.lstat()
                    if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
                        continue
                    relative = path.relative_to(root).as_posix()
                    key = kind + "/" + relative
                    # Android/Linux ctime captures files copied with an old mtime.
                    # Windows ctime denotes birth time and is not a change cursor.
                    scan_ns = info.st_ctime_ns if os.name != "nt" else info.st_mtime_ns
                    if (scan_ns, key) <= (since_ns, after) or scan_ns > until_ns:
                        continue
                    mime, status = self.describe(path, kind)
                    entries.append({"account": account, "kind": kind, "relative_path": relative,
                                    "key": key, "scan_ns": scan_ns, "mtime_ns": info.st_mtime_ns,
                                    "ctime_ns": info.st_ctime_ns if os.name != "nt" else 0, "size": info.st_size,
                                    "mime_type": mime, "representation": status,
                                    "chat_id": None, "association": "unresolved"})
        entries.sort(key=lambda item: (item["scan_ns"], item["key"]))
        page = entries[:limit]
        cursor = {"mtime_ns": page[-1]["scan_ns"], "after": page[-1]["key"]} if page else {"mtime_ns": since_ns, "after": after}
        return {"ok": True, "account": account, "items": page, "next_cursor": cursor,
                "has_more": len(entries) > limit, "until_ns": until_ns,
                "cursor_version": 2, "edge_time_ns": time.time_ns()}

    def read(self, account: str, kind: str, relative_path: str, mtime_ns: int, size: int,
             ctime_ns: int = 0,
             max_bytes: int = 64 * 1024 * 1024) -> Tuple[bytes, str]:
        roots = self.roots(account)
        if account != roots["image"].parent.name or kind not in roots:
            raise ValueError("账号已切换或媒体类型无效")
        root = roots[kind]
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("媒体路径必须位于授权目录内")
        path = root / relative
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("拒绝越界路径或符号链接")
        if size <= 0 or size > max_bytes:
            raise ValueError("文件超出单次传输限制；保留游标等待处理")
        with path.open("rb") as source:
            before = os.fstat(source.fileno())
            if ((before.st_mtime_ns, before.st_size) != (mtime_ns, size)
                    or (ctime_ns and before.st_ctime_ns != ctime_ns)):
                raise ValueError("扫描后文件发生变化，请重新扫描")
            data = source.read(max_bytes + 1)
            after = os.fstat(source.fileno())
        if ((after.st_mtime_ns, after.st_size) != (mtime_ns, size)
                or (ctime_ns and after.st_ctime_ns != ctime_ns) or len(data) != size):
            raise ValueError("读取时文件发生变化，请重新扫描")
        return data, hashlib.sha256(data).hexdigest()

    @staticmethod
    def voice_wav(data: bytes) -> bytes:
        """SILK 解码为 24kHz/16bit/mono PCM，原样封装 WAV，不再有损转码。"""
        if data.startswith(b"\x02#!SILK_V3"):
            data = data[1:]
        if not data.startswith(b"#!SILK_V3"):
            raise ValueError("不是 SILK V3 原件")
        with tempfile.TemporaryDirectory(prefix="maibot-wav-", dir="/data/local/tmp") as directory:
            root = Path(directory)
            (root / "original.silk").write_bytes(data)
            env = dict(os.environ, LD_LIBRARY_PATH="/data/data/com.termux/files/usr/lib")
            subprocess.run(["/data/adb/maibot_edge/bin/silk_decoder", str(root / "original.silk"),
                            str(root / "voice.pcm"), "-Fs_API", "24000"],
                           check=True, capture_output=True, env=env, timeout=60)
            pcm = (root / "voice.pcm").read_bytes()
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(24000)
                output.writeframes(pcm)
            return buffer.getvalue()

    @staticmethod
    def voice_ogg(data: bytes) -> bytes:
        if data.startswith(b"\x02#!SILK_V3"):
            data = data[1:]
        if not data.startswith(b"#!SILK_V3"):
            raise ValueError("不是 SILK V3 原件")
        with tempfile.TemporaryDirectory(prefix="maibot-silk-", dir="/data/local/tmp") as directory:
            root = Path(directory)
            (root / "original.silk").write_bytes(data)
            env = dict(os.environ, LD_LIBRARY_PATH="/data/data/com.termux/files/usr/lib")
            subprocess.run(["/data/adb/maibot_edge/bin/silk_decoder", str(root / "original.silk"), str(root / "voice.pcm")],
                           check=True, capture_output=True, env=env, timeout=60)
            subprocess.run(["/data/data/com.termux/files/usr/bin/ffmpeg", "-y", "-f", "s16le", "-ar", "24000",
                            "-ac", "1", "-i", str(root / "voice.pcm"), "-c:a", "libopus", str(root / "voice.ogg")],
                           check=True, capture_output=True, env=env, timeout=60)
            return (root / "voice.ogg").read_bytes()
