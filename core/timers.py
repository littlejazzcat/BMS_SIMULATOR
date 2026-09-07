"""周期计时器：demo 阶段用于充电期报文的周期发送判断"""

import time


class PeriodTimer:
    """简单周期计时器，判断距上次触发是否超过周期"""

    def __init__(self, period_ms: float):
        self.period_ms = period_ms
        self._last = 0.0

    def ready(self) -> bool:
        """距上次触发超过周期则返回 True 并重置"""
        now = time.monotonic()
        if now - self._last >= self.period_ms / 1000.0:
            self._last = now
            return True
        return False

    def reset(self) -> None:
        self._last = time.monotonic()
