"""ZlgTransport：周立功 USBCAN 真实 CAN 连接。

封装 zlgcan 库，实现 CanTransport 接口，与 MockTransport 可互换。
"""

import ctypes
import threading
import time

from zlgcan import (
    ZCAN,
    ZCAN_USBCAN_2E_U, ZCAN_USBCAN_E_U, ZCAN_USBCAN_4E_U, ZCAN_USBCAN_8E_U,
    ZCAN_TYPE_CAN, ZCAN_STATUS_OK,
    INVALID_DEVICE_HANDLE, INVALID_CHANNEL_HANDLE,
    ZCAN_CHANNEL_INIT_CONFIG, ZCAN_Transmit_Data,
)

from transport.base import CanTransport, CanFrame

# 前端 device_type 字符串 → ZCAN 设备类型常量
DEVICE_TYPE_MAP = {
    "USBCAN-E-U": ZCAN_USBCAN_E_U,
    "USBCAN-2E-U": ZCAN_USBCAN_2E_U,
    "USBCAN-4E-U": ZCAN_USBCAN_4E_U,
    "USBCAN-8E-U": ZCAN_USBCAN_8E_U,
}


class ZlgTransport(CanTransport):
    def __init__(self):
        self.zcanlib = ZCAN()
        self.device_handle = None
        self.chn_handle = None
        self._connected = False
        self._running = False
        self._thread = None
        self._callbacks = []
        self._lock = threading.Lock()
        self._callback_lock = threading.Lock()
        self._tx_count = 0
        self._rx_count = 0
        self._last_error = None
        self._device_type_str = "USBCAN-2E-U"
        self._channel = 0
        self._baudrate = 250000

    # ── CanTransport 接口 ──────────────────────────

    def connect(self, config: dict = None) -> bool:
        config = config or {}
        self._device_type_str = config.get("device_type", "USBCAN-2E-U")
        self._channel = int(config.get("channel", 0))
        self._baudrate = int(config.get("baudrate", 250000))
        dev_type = DEVICE_TYPE_MAP.get(self._device_type_str, ZCAN_USBCAN_2E_U)

        try:
            # 1. 打开设备
            self.device_handle = self.zcanlib.OpenDevice(dev_type, 0, 0)
            if self.device_handle == INVALID_DEVICE_HANDLE:
                self._last_error = "打开设备失败，请检查 CAN 盒连接和驱动"
                return False

            # 2. 设置波特率
            ret = self.zcanlib.ZCAN_SetValue(
                self.device_handle,
                f"{self._channel}/baud_rate",
                str(self._baudrate).encode("utf-8"),
            )
            if ret != ZCAN_STATUS_OK:
                self._last_error = f"设置波特率失败"
                return False

            # 3. 初始化通道
            cfg = ZCAN_CHANNEL_INIT_CONFIG()
            cfg.can_type = ZCAN_TYPE_CAN
            cfg.config.canfd.mode = 0  # 正常模式（非只听）
            self.chn_handle = self.zcanlib.InitCAN(self.device_handle, self._channel, cfg)
            if self.chn_handle is None or self.chn_handle == INVALID_CHANNEL_HANDLE:
                self._last_error = "初始化通道失败"
                return False

            # 4. 启动通道
            ret = self.zcanlib.StartCAN(self.chn_handle)
            if ret != ZCAN_STATUS_OK:
                self._last_error = "启动通道失败"
                return False

            # 5. 清除过滤规则（接收所有报文）
            self.zcanlib.ZCAN_SetValue(
                self.device_handle, f"{self._channel}/filter_clear", "0".encode("utf-8"))

            # 6. 启动接收线程
            self._running = True
            self._connected = True
            self._last_error = None
            self._thread = threading.Thread(target=self._receive_loop, daemon=True)
            self._thread.start()
            return True
        except Exception as e:
            self._last_error = str(e)
            return False

    def disconnect(self) -> None:
        self._running = False
        self._connected = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        try:
            if self.chn_handle:
                self.zcanlib.ResetCAN(self.chn_handle)
        except Exception:
            pass
        try:
            if self.device_handle:
                self.zcanlib.CloseDevice(self.device_handle)
        except Exception:
            pass
        self.device_handle = None
        self.chn_handle = None

    def reset(self) -> None:
        # 真实 CAN 无需复位，充电机会自己重新握手
        pass

    def send(self, frame: CanFrame) -> bool:
        if not self._connected or not self.chn_handle:
            return False
        try:
            msgs = (ZCAN_Transmit_Data * 1)()
            ctypes.memset(ctypes.addressof(msgs), 0, ctypes.sizeof(msgs))
            msgs[0].transmit_type = 0
            can_id = frame.can_id
            if frame.is_extended:
                can_id |= 1 << 31  # bit31 = 扩展帧标志
            msgs[0].frame.can_id = can_id
            msgs[0].frame.can_dlc = len(frame.data)
            msgs[0].frame._pad |= 0x20  # 发送回显
            for j in range(len(frame.data)):
                msgs[0].frame.data[j] = frame.data[j]
            ret = self.zcanlib.Transmit(self.chn_handle, msgs, 1)
            with self._lock:
                self._tx_count += 1
            return ret == 1
        except Exception as e:
            self._last_error = str(e)
            return False

    def register_callback(self, cb) -> None:
        with self._callback_lock:
            if cb not in self._callbacks:
                self._callbacks.append(cb)

    def get_status(self) -> dict:
        return {
            "connected": self._connected,
            "device_type": self._device_type_str,
            "channel": self._channel,
            "baudrate": self._baudrate,
            "tx_count": self._tx_count,
            "rx_count": self._rx_count,
            "last_error": self._last_error,
        }

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ── 接收线程 ──────────────────────────────────

    def _receive_loop(self):
        while self._running:
            try:
                rcv_num = self.zcanlib.GetReceiveNum(self.chn_handle, ZCAN_TYPE_CAN)
                if rcv_num <= 0:
                    time.sleep(0.002)
                    continue
                read_num = min(rcv_num, 100)
                rcv_msg, actual_num = self.zcanlib.Receive(self.chn_handle, read_num, 50)
                if actual_num <= 0:
                    continue
                for msg in rcv_msg[:actual_num]:
                    frame = msg.frame
                    is_echo = (frame._pad & 0x20) != 0  # 发送回显帧（自己发的）
                    can_id = frame.can_id & 0x1FFFFFFF   # 取低 29 位实际 ID
                    dlc = frame.can_dlc
                    data = bytes(frame.data[:dlc]) if dlc > 0 else b""
                    if is_echo:
                        # 回显帧跳过：TX 显示由 controller 的 frame_tx 事件处理
                        continue
                    cf = CanFrame(can_id=can_id, data=data, is_extended=True,
                                  timestamp=time.time(), direction="rx")
                    with self._lock:
                        self._rx_count += 1
                    self._notify(cf)
            except Exception:
                time.sleep(0.005)

    def _notify(self, frame: CanFrame):
        with self._callback_lock:
            cbs = list(self._callbacks)
        for cb in cbs:
            try:
                cb(frame)
            except Exception:
                pass
