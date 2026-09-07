"""MockTransport：内置迷你充电机（SECC）状态机，无真桩时离线跑全链路。

动态模拟，非静态回放：
    收到 BHM    → 回 CRM=00
    收到 BRM    → 回 CRM=AA
    收到 BCP    → 回 CML
    收到 BRO=AA → 回 CRO=AA，进入充电态
    充电态      → 每 50ms 周期发 CCS，持续一段时间后发 CST 结束
    收到 BST    → 回 CST 结束
"""

import threading
import time

from transport.base import CanTransport, CanFrame


class MockTransport(CanTransport):
    def __init__(self, codec, charging_duration: float = 8.0):
        self.codec = codec
        self.protocol = codec.protocol
        self._charging_duration = charging_duration

        self._connected = False
        self._running = False
        self._thread = None

        self._callbacks = []
        self._callback_lock = threading.Lock()
        self._lock = threading.Lock()

        self._tx_count = 0          # BMS 发出的帧数
        self._rx_count = 0          # BMS 收到的帧数（Mock 发出）
        self._last_error = None

        # 迷你充电机状态机
        self._secc_state = "SENDING_CHM"
        self._last_chm = 0.0
        self._last_ccs = 0.0
        self._charging_start = 0.0

    # ── CanTransport 接口 ──────────────────────────

    def connect(self, config: dict = None) -> bool:
        if self._connected:
            return True
        self._connected = True
        self._running = True
        self._secc_state = "SENDING_CHM"
        self._last_chm = 0.0
        self._last_ccs = 0.0
        self._thread = threading.Thread(target=self._secc_loop, daemon=True)
        self._thread.start()
        return True

    def disconnect(self) -> None:
        self._running = False
        self._connected = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def reset(self) -> None:
        """复位迷你充电机状态机，重新开始充电流程"""
        self._secc_state = "SENDING_CHM"
        self._last_chm = 0.0
        self._last_ccs = 0.0
        self._charging_start = 0.0

    def send(self, frame: CanFrame) -> bool:
        """BMS 发出帧，Mock 收到后触发充电机响应"""
        frame.direction = "tx"
        frame.timestamp = time.time()
        with self._lock:
            self._tx_count += 1
        self._handle_bms_frame(frame)
        return True

    def register_callback(self, cb) -> None:
        with self._callback_lock:
            if cb not in self._callbacks:
                self._callbacks.append(cb)

    def get_status(self) -> dict:
        return {
            "connected": self._connected,
            "device_type": "Mock",
            "channel": 0,
            "baudrate": 250000,
            "tx_count": self._tx_count,
            "rx_count": self._rx_count,
            "last_error": self._last_error,
        }

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ── 迷你充电机状态机 ──────────────────────────

    def _secc_loop(self):
        """周期发送：握手启动阶段发 CHM，充电阶段发 CCS"""
        while self._running:
            now = time.time()
            if self._secc_state == "SENDING_CHM":
                if now - self._last_chm >= 0.25:
                    self._emit_chm()
            elif self._secc_state == "CHARGING":
                if now - self._last_ccs >= 0.05:
                    self._emit_ccs()
                if now - self._charging_start >= self._charging_duration:
                    self._emit_cst()
                    self._secc_state = "ENDED"
            time.sleep(0.01)

    def _handle_bms_frame(self, frame: CanFrame):
        """收到 BMS 报文，推进充电机状态机"""
        pf = (frame.can_id >> 16) & 0xFF

        # TP 控制帧（RTS）：BRM/BCP 等多包报文的起始帧，从 PGN 识别
        if pf == 0xEC and frame.data and frame.data[0] == 0x10:
            pgn = frame.data[5] | (frame.data[6] << 8) | (frame.data[7] << 16)
            tmsg = self.protocol.get_message_by_pf(pgn >> 8)
            if tmsg and tmsg.abbr == "BRM":
                self._emit_crm(0xAA)
                self._secc_state = "WAITING_BCP"
            elif tmsg and tmsg.abbr == "BCP":
                self._emit_cml()
                self._secc_state = "WAITING_BRO"
            return

        msg = self.codec.identify_message(frame.can_id, frame.data)
        if msg is None:
            return
        abbr = msg.abbr

        if abbr == "BHM":
            self._emit_crm(0x00)
            self._secc_state = "WAITING_BRM"
        elif abbr == "BRO":
            if frame.data and frame.data[0] == 0xAA:
                self._emit_cro(0xAA)
                self._secc_state = "CHARGING"
                self._charging_start = time.time()
                self._last_ccs = time.time()
        elif abbr == "BST":
            self._emit_cst()
            self._secc_state = "ENDED"

    # ── 构造充电机报文 ────────────────────────────

    def _emit(self, abbr: str, params: dict):
        """用 codec 编码一帧充电机报文并推送给回调"""
        msg = self.protocol.get_message_by_abbr(abbr)
        r = self.codec.encode(msg, params)
        frame = CanFrame(
            can_id=r["can_id"],
            data=r["data"],
            timestamp=time.time(),
            direction="rx",
        )
        with self._lock:
            self._rx_count += 1
        self._notify(frame)

    def _notify(self, frame: CanFrame):
        with self._callback_lock:
            cbs = list(self._callbacks)
        for cb in cbs:
            try:
                cb(frame)
            except Exception:
                pass

    def _emit_chm(self):
        self._last_chm = time.time()
        self._emit("CHM", {"充电机通信协议版本号": 0x000101})  # V1.1 → 01 01 00

    def _emit_crm(self, result: int):
        if result == 0xAA:
            self._emit("CRM", {"辨识结果": "已辨识(0xAA)", "充电机编号": 1})
        else:
            self._emit("CRM", {"辨识结果": "未辨识", "充电机编号": 1})

    def _emit_cml(self):
        self._emit("CML", {
            "最高充电电压": 500.0,
            "最低充电电压": 200.0,
            "最大充电电流": 250.0,
            "最小充电电流": 10.0,
        })

    def _emit_cro(self, result: int):
        self._emit("CRO", {
            "充电机充电准备好": "完成准备(0xAA)" if result == 0xAA else "未完成准备"
        })

    def _emit_ccs(self):
        self._last_ccs = time.time()
        self._emit("CCS", {
            "车辆接口当前电压测量值": 400.0,
            "车辆接口当前电流测量值": 180.0,
            "累计充电时间": 0,
            "充电允许": "允许",
        })

    def _emit_cst(self):
        self._emit("CST", {"达到设定条件": "中止"})
