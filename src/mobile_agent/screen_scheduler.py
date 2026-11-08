"""Heartflow 屏幕占用状态；同步写库，异步调用方须使用 asyncio.to_thread。"""

from typing import Any, Dict, List, Literal, Optional, Sequence
import time

from src.companion_vault.consolidator import update_body_state
from src.companion_vault.runtime_bridge import DEFAULT_TRUTH_DB_PATH

ScreenState = Literal["IDLE", "AUTONOMOUS_SURFING", "BACKGROUND_FAST_REPLY",
                      "WECHAT_ACTIVE_SESSION", "WAITING_USER_CONFIRM"]


class ScreenScheduler:
    def __init__(self, db_path: str = DEFAULT_TRUTH_DB_PATH, cooldown_seconds: float = 180.0) -> None:
        self.db_path = db_path
        self.state: ScreenState = "IDLE"
        self.current_app = ""
        self.suspended_surfing_app = ""
        self.last_wechat_active_at: float = 0.0
        self.cooldown_seconds = cooldown_seconds
        self.latest_screen_file_code = ""
        self._confirmation_state: ScreenState = "IDLE"

    def _transition(self, state: ScreenState, app: str, observation: str) -> str:
        update_body_state(self.db_path, body_state=state, current_app=app, live_observation=observation,
                          latest_screen_file_code=self.latest_screen_file_code)
        self.state, self.current_app = state, app
        return self.state

    def start_surfing(self, app: str) -> str:
        if self.state != "IDLE":
            raise RuntimeError("只有空闲时可以启动自主冲浪")
        if not app:
            raise ValueError("冲浪应用不能为空")
        return self._transition("AUTONOMOUS_SURFING", app, "开始自主冲浪")

    def on_incoming_wechat_message(self, *, requires_foreground: bool, sender_name: str, preview: str) -> str:
        if self.state == "WAITING_USER_CONFIRM":
            # 人工确认锁不能被自动消息抢占解除；消息消费方在确认后重试。
            return self.state
        observation = f"微信消息，{sender_name}: {preview}"
        if not requires_foreground and self.state in {"AUTONOMOUS_SURFING", "BACKGROUND_FAST_REPLY"}:
            return self._transition("BACKGROUND_FAST_REPLY", self.current_app, observation)
        suspended = self.current_app if self.state in {"AUTONOMOUS_SURFING", "BACKGROUND_FAST_REPLY"} else self.suspended_surfing_app
        state = self._transition("WECHAT_ACTIVE_SESSION", "com.tencent.mm", observation)
        self.suspended_surfing_app = suspended
        self.last_wechat_active_at = time.time()
        return state

    def finish_background_reply(self) -> str:
        if self.state != "BACKGROUND_FAST_REPLY":
            raise RuntimeError("当前不在后台回复状态")
        return self._transition("AUTONOMOUS_SURFING", self.current_app, "后台回复完成，继续自主冲浪")

    def wait_for_user_confirm(self, observation: str) -> str:
        if self.state != "WAITING_USER_CONFIRM":
            previous = self.state
            self._transition("WAITING_USER_CONFIRM", self.current_app, observation)
            self._confirmation_state = previous
        return self.state

    def resume_after_user_confirm(self) -> str:
        if self.state != "WAITING_USER_CONFIRM":
            raise RuntimeError("当前没有待确认操作")
        state = self._transition(self._confirmation_state, self.current_app, "用户已确认，恢复任务")
        if state == "WECHAT_ACTIVE_SESSION":
            self.last_wechat_active_at = time.time()
        return state

    def tick_cooldown(self, now: Optional[float] = None) -> str:
        timestamp = time.time() if now is None else now
        if self.state == "WECHAT_ACTIVE_SESSION" and timestamp-self.last_wechat_active_at >= self.cooldown_seconds:
            app = self.suspended_surfing_app
            self._transition("AUTONOMOUS_SURFING" if app else "IDLE", app, "微信会话冷却结束")
            self.suspended_surfing_app = ""
        return self.state


def compress_slow_track_history(steps: Sequence[Dict[str, Any]], keep_recent_screenshots: int = 2) -> List[Dict[str, Any]]:
    if keep_recent_screenshots < 0:
        raise ValueError("截图保留步数不能为负")
    cutoff = len(steps)-keep_recent_screenshots
    return [dict(step) if index >= cutoff else
            {key: step[key] for key in ("step_index", "action", "observation_summary") if key in step}
            for index, step in enumerate(steps)]
