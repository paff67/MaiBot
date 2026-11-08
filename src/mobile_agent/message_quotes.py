"""Compatibility entrypoint for platform_io.wechat."""
from src.platform_io.wechat.message_quotes import app_message, ensure_quote_schema, latest_quote, parse_quote, safe_body, save_quote

__all__ = ['app_message', 'ensure_quote_schema', 'latest_quote', 'parse_quote', 'safe_body', 'save_quote']
