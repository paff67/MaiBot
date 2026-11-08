"""Compatibility launcher for the integration harness, not a production brain."""
from src.integration_tools.wechat_live_bridge import WeChatLiveBridge, main, time

__all__ = ["WeChatLiveBridge", "main", "time"]

if __name__ == "__main__":
    main()
