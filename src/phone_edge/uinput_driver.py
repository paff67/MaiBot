"""Linux Type-B 单指触控；含 sleep 的方法须在工作线程调用。"""

from typing import Optional, Tuple
import fcntl
import os
import random
import struct
import time

EV_SYN = 0x00
EV_KEY = 0x01
EV_ABS = 0x03
SYN_REPORT = 0
BTN_TOOL_FINGER = 0x145
BTN_TOUCH = 0x14A
INPUT_PROP_DIRECT = 0x01
ABS_MT_SLOT = 0x2F
ABS_MT_TOUCH_MAJOR = 0x30
ABS_MT_WIDTH_MAJOR = 0x32
ABS_MT_POSITION_X = 0x35
ABS_MT_POSITION_Y = 0x36
ABS_MT_TRACKING_ID = 0x39
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_ABSBIT = 0x40045567
UI_SET_PROPBIT = 0x4004556E
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502


class UInputTouchDriver:
    def __init__(
        self,
        width: int = 1080,
        height: int = 2340,
        device_path: str = "/dev/input/event5",
    ) -> None:
        if width <= 0 or height <= 0:
            raise ValueError("触摸屏尺寸必须为正数")
        self.width = width
        self.height = height
        self._tracking_id = 100
        if device_path == "/dev/input/event5" and not os.path.exists(device_path):
            device_path = "/dev/uinput"
        self._is_uinput = device_path == "/dev/uinput"
        self.fd = os.open(device_path, os.O_WRONLY)
        if not self._is_uinput:
            return
        created = False
        try:
            for event_type in (EV_SYN, EV_KEY, EV_ABS):
                fcntl.ioctl(self.fd, UI_SET_EVBIT, event_type)
            fcntl.ioctl(self.fd, UI_SET_KEYBIT, BTN_TOUCH)
            fcntl.ioctl(self.fd, UI_SET_KEYBIT, BTN_TOOL_FINGER)
            fcntl.ioctl(self.fd, UI_SET_PROPBIT, INPUT_PROP_DIRECT)
            maxima = [0] * 64
            for axis, maximum in ((ABS_MT_SLOT, 9), (ABS_MT_TOUCH_MAJOR, 1080), (ABS_MT_WIDTH_MAJOR, 127),
                                  (ABS_MT_POSITION_X, width - 1), (ABS_MT_POSITION_Y, height - 1),
                                  (ABS_MT_TRACKING_ID, 65535)):
                fcntl.ioctl(self.fd, UI_SET_ABSBIT, axis)
                maxima[axis] = maximum
            descriptor = struct.pack("=80sHHHHI", b"MaiBot direct touch", 0x06, 1, 1, 1, 0)
            descriptor += struct.pack("=256i", *(maxima + [0] * 192))
            self._write(descriptor)
            fcntl.ioctl(self.fd, UI_DEV_CREATE)
            created = True
        finally:
            if not created:
                os.close(self.fd)
        time.sleep(0.2)

    def _write(self, data: bytes) -> None:
        if os.write(self.fd, data) != len(data):
            raise OSError("uinput 写入不完整")

    def _event(self, event_type: int, code: int, value: int) -> None:
        self._write(struct.pack("@llHHi", 0, 0, event_type, code, value))

    def _clip(self, x: float, y: float) -> Tuple[int, int]:
        return max(0, min(self.width - 1, round(x))), max(0, min(self.height - 1, round(y)))

    def _move(self, x: int, y: int) -> None:
        self._event(EV_ABS, ABS_MT_POSITION_X, x)
        self._event(EV_ABS, ABS_MT_POSITION_Y, y)
        self._event(EV_SYN, SYN_REPORT, 0)

    def _down(self, x: int, y: int) -> None:
        self._tracking_id = (self._tracking_id + 1) % 65535 + 1
        self._event(EV_ABS, ABS_MT_SLOT, 0)
        self._event(EV_ABS, ABS_MT_TRACKING_ID, self._tracking_id)
        self._event(EV_ABS, ABS_MT_POSITION_X, x)
        self._event(EV_ABS, ABS_MT_POSITION_Y, y)
        self._event(EV_ABS, ABS_MT_TOUCH_MAJOR, 8)
        self._event(EV_ABS, ABS_MT_WIDTH_MAJOR, 8)
        self._event(EV_KEY, BTN_TOUCH, 1)
        self._event(EV_KEY, BTN_TOOL_FINGER, 1)
        self._event(EV_SYN, SYN_REPORT, 0)

    def _up(self) -> None:
        self._event(EV_ABS, ABS_MT_SLOT, 0)
        self._event(EV_ABS, ABS_MT_TRACKING_ID, -1)
        self._event(EV_KEY, BTN_TOUCH, 0)
        self._event(EV_KEY, BTN_TOOL_FINGER, 0)
        self._event(EV_SYN, SYN_REPORT, 0)

    def tap(self, x: int, y: int, jitter_std: float = 3.5, hold_ms: Optional[int] = None) -> Tuple[int, int]:
        duration = random.randint(45, 95) if hold_ms is None else hold_ms
        if jitter_std < 0 or duration < 0:
            raise ValueError("偏移标准差和按压时长不能为负")
        point = self._clip(random.gauss(x, jitter_std), random.gauss(y, jitter_std))
        self._down(*point)
        try:
            time.sleep(duration / 1000)
        finally:
            self._up()
        return point

    def long_press(self, x: int, y: int, duration_ms: int = 650) -> Tuple[int, int]:
        return self.tap(x, y, hold_ms=duration_ms)

    def bezier_swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 320, steps: int = 24) -> None:
        if duration_ms <= 0 or steps < 2:
            raise ValueError("滑动时长必须为正，轨迹至少两帧")
        start, end = self._clip(x1, y1), self._clip(x2, y2)
        cx = (start[0] + end[0]) / 2 + random.gauss(0, 35)
        cy = (start[1] + end[1]) / 2 + random.gauss(0, 35)
        self._down(*start)
        began = time.monotonic()
        try:
            for index in range(1, steps + 1):
                t = index / steps
                point = self._clip((1-t)**2 * start[0] + 2*(1-t)*t*cx + t*t*end[0],
                                   (1-t)**2 * start[1] + 2*(1-t)*t*cy + t*t*end[1])
                time.sleep(max(0, began + t * duration_ms / 1000 - time.monotonic()))
                self._move(*point)
        finally:
            self._up()

    def close(self) -> None:
        if self.fd != -1:
            try:
                if self._is_uinput:
                    fcntl.ioctl(self.fd, UI_DEV_DESTROY)
            finally:
                os.close(self.fd)
                self.fd = -1
