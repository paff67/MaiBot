"""Compatibility entrypoint for platform_io.wechat."""
from src.platform_io.wechat.message_ingestor import MessageLedger, main, poll

__all__ = ['MessageLedger', 'main', 'poll']

if __name__ == "__main__":
    main()
