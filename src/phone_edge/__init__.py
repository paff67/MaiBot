"""小米 10 APatch 边缘端；触控和文件接口均为同步接口。"""

from .watcher_daemon import find_active_wechat_account_dir, inspect_wechat_image_magic, strip_wechat_silk_header


def __getattr__(name):
    # 文件读取工具不依赖 Linux fcntl；仅请求触控驱动时加载平台专用模块。
    if name == "UInputTouchDriver":
        from .uinput_driver import UInputTouchDriver
        return UInputTouchDriver
    raise AttributeError(name)

# 保持服务模块延迟导入，支持 python3 -m maibot_edge.edge_server 独立启动。
__all__ = [
    "edge_server",
    "UInputTouchDriver",
    "find_active_wechat_account_dir",
    "inspect_wechat_image_magic",
    "strip_wechat_silk_header",
]
