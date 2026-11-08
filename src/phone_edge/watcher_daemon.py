"""微信账号目录及媒体格式识别；不读取或解密微信数据库。"""

from pathlib import Path
from typing import Optional, Tuple
import os
import re
import subprocess


def find_active_wechat_account_dir(micromsg_root: str = "/data/data/com.tencent.mm/MicroMsg") -> Path:
    candidates = [path for path in Path(micromsg_root).iterdir()
                  if re.fullmatch(r"[0-9a-fA-F]{32}", path.name) and path.is_dir()
                  and (path / "voice2").is_dir() and (path / "image2").is_dir()]
    if not candidates:
        raise FileNotFoundError(f"未找到微信账号目录: {micromsg_root}")
    # 目录 mtime 只反映子项增删，不能代表当前聊天活跃账号。优先比较消息数据库写入时间。
    return max(candidates, key=lambda path: (path / "EnMicroMsg.db").stat().st_mtime_ns
               if (path / "EnMicroMsg.db").is_file() else path.stat().st_mtime_ns)


def inspect_wechat_image_magic(file_path: str, min_bytes: int = 20480) -> Optional[str]:
    path = Path(file_path)
    if path.stat().st_size < min_bytes:
        return None
    with path.open("rb") as source:
        magic = source.read(8)
    if magic.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if magic.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    return None


def strip_wechat_silk_header(amr_bytes: bytes) -> bytes:
    if not amr_bytes.startswith(b"\x02#!SILK_V3"):
        raise ValueError("不是微信 SILK_V3 语音头")
    return amr_bytes[1:]


def find_latest_wechat_voice(micromsg_root: str = "/data/data/com.tencent.mm/MicroMsg") -> Path:
    account = find_active_wechat_account_dir(micromsg_root)
    voices = [path for path in (account / "voice2").rglob("msg_*.amr") if path.is_file()]
    if not voices:
        raise FileNotFoundError(f"未找到微信语音: {account / 'voice2'}")
    return max(voices, key=lambda path: path.stat().st_mtime_ns)


def transcode_silk_to_ogg(
    amr_path: str,
    decoder_bin: str = "/data/adb/maibot_edge/bin/silk_decoder",
    ffmpeg_bin: str = "/data/data/com.termux/files/usr/bin/ffmpeg",
    termux_lib: str = "/data/data/com.termux/files/usr/lib",
) -> bytes:
    """固定临时文件由 HTTP 服务的 voice_lock 串行保护。"""
    silk_path = Path("/data/local/tmp/edge_voice.silk")
    pcm_path = Path("/data/local/tmp/edge_voice.pcm")
    ogg_path = Path("/data/local/tmp/edge_voice.ogg")
    silk_path.write_bytes(strip_wechat_silk_header(Path(amr_path).read_bytes()))
    env = dict(os.environ, LD_LIBRARY_PATH=termux_lib)
    subprocess.run([decoder_bin, str(silk_path), str(pcm_path)], check=True, env=env)
    subprocess.run(
        [ffmpeg_bin, "-y", "-f", "s16le", "-ar", "24000", "-ac", "1",
         "-i", str(pcm_path), "-c:a", "libopus", "-b:a", "32k", str(ogg_path)],
        check=True, env=env,
    )
    return ogg_path.read_bytes()


def find_latest_wechat_image(
    micromsg_root: str = "/data/data/com.tencent.mm/MicroMsg", min_bytes: int = 20480,
) -> Optional[Tuple[Path, str]]:
    account = find_active_wechat_account_dir(micromsg_root)
    images = sorted(
        (path for path in (account / "image2").rglob("*") if path.is_file()),
        key=lambda path: path.stat().st_mtime_ns, reverse=True,
    )
    for path in images:
        mime_type = inspect_wechat_image_magic(str(path), min_bytes)
        if mime_type is not None:
            return path, mime_type
    return None
