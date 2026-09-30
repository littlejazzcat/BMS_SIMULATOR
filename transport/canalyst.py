"""CanalystTransport：创芯科技 CANalyst-II 真实 CAN 连接。

使用 ControlCAN.dll（VCI_* 接口），实现 CanTransport 接口，与 MockTransport/ZlgTransport 可互换。
注意：ControlCAN.dll 需与 Python 位数一致（本项目为 64 位 Python，需用 64 位 DLL）。
"""

import ctypes
import threading
import time
from pathlib import Path

from transport.base import CanTransport, CanFrame
from paths import app_dir, resource_path

# 设备类型：CANalyst-II / USBCAN-2A / USBCAN-2C 等均为 VCI_USBCAN2 = 4
VCI_USBCAN2 = 4

# 波特率 → (Timing0, Timing1)，取自创芯《接口函数库使用说明书》V2.10
BAUDRATE_MAP = {
    20000: (0x18, 0x1C),
    40000: (0x87, 0xFF),
    50000: (0x09, 0x1C),
    80000: (0x83, 0xFF),
    100000: (0x04, 0x1C),
    125000: (0x03, 0x1C),
    200000: (0x81, 0xFA),
    250000: (0x01, 0x1C),
    400000: (0x80, 0xFA),
    500000: (0x00, 0x1C),
    666000: (0x80, 0xB6),
    800000: (0x00, 0x16),
    1000000: (0x00, 0x14),
}

RECV_BUF_SIZE = 2500


class VCI_INIT_CONFIG(ctypes.Structure):
    _fields_ = [
        ("AccCode", ctypes.c_uint),
        ("AccMask", ctypes.c_uint),
        ("Reserved", ctypes.c_uint),
        ("Filter", ctypes.c_ubyte),
        ("Timing0", ctypes.c_ubyte),
        ("Timing1", ctypes.c_ubyte),
        ("Mode", ctypes.c_ubyte),
    ]


class VCI_BOARD_INFO(ctypes.Structure):
    _fields_ = [
        ("hw_Version", ctypes.c_ushort),
        ("fw_Version", ctypes.c_ushort),
        ("dr_Version", ctypes.c_ushort),
        ("in_Version", ctypes.c_ushort),
        ("irq_Num", ctypes.c_ushort),
        ("can_Num", ctypes.c_ubyte),
        ("str_Serial_Num", ctypes.c_char * 20),
        ("str_hw_Type", ctypes.c_char * 40),
        ("Reserved", ctypes.c_ushort * 4),
    ]


class VCI_CAN_OBJ(ctypes.Structure):
    _fields_ = [
        ("ID", ctypes.c_uint),
        ("TimeStamp", ctypes.c_uint),
        ("TimeFlag", ctypes.c_ubyte),
        ("SendType", ctypes.c_ubyte),
        ("RemoteFlag", ctypes.c_ubyte),
        ("ExternFlag", ctypes.c_ubyte),
        ("DataLen", ctypes.c_ubyte),
        ("Data", ctypes.c_ubyte * 8),
        ("Reserved", ctypes.c_ubyte * 3),
    ]


class CanalystTransport(CanTransport):
    def __init__(self):
        self._dll = None
        self._dev_type = VCI_USBCAN2
        self._dev_index = 0
        self._channel = 0
        self._baudrate = 250000
        self._connected = False
        self._running = False
        self._thread = None
        self._callbacks = []
        self._disconnect_cbs = []
        self._lock = threading.Lock()
        self._callback_lock = threading.Lock()
        self._tx_count = 0
        self._rx_count = 0
        self._last_error = None

    # ── 内部 ──────────────────────────────────────

    def _load_dll(self) -> bool:
        """懒加载 ControlCAN.dll（64 位），并声明函数签名"""
        if self._dll is not None:
            return True
        try:
            # DLL 与 zlgcan.dll 一同放在 exe 旁边（开发时为项目根）
            dll_path = str(app_dir() / "ControlCAN.dll")
            if not Path(dll_path).exists():
                dll_path = resource_path("ControlCAN.dll")  # 兼容旧布局（_internal）
            self._dll = ctypes.WinDLL(dll_path)
            u, i, pobj = ctypes.c_uint, ctypes.c_int, ctypes.POINTER(VCI_CAN_OBJ)
            self._dll.VCI_OpenDevice.argtypes = [u, u, u]
            self._dll.VCI_OpenDevice.restype = u
            self._dll.VCI_CloseDevice.argtypes = [u, u]
            self._dll.VCI_CloseDevice.restype = u
            self._dll.VCI_InitCAN.argtypes = [u, u, u, ctypes.POINTER(VCI_INIT_CONFIG)]
            self._dll.VCI_InitCAN.restype = u
            self._dll.VCI_StartCAN.argtypes = [u, u, u]
            self._dll.VCI_StartCAN.restype = u
            self._dll.VCI_ResetCAN.argtypes = [u, u, u]
            self._dll.VCI_ResetCAN.restype = u
            self._dll.VCI_Transmit.argtypes = [u, u, u, pobj, u]
            self._dll.VCI_Transmit.restype = u
            self._dll.VCI_Receive.argtypes = [u, u, u, pobj, u, i]
            self._dll.VCI_Receive.restype = u
            self._dll.VCI_GetReceiveNum.argtypes = [u, u, u]
            self._dll.VCI_GetReceiveNum.restype = u
            self._dll.VCI_ClearBuffer.argtypes = [u, u, u]
            self._dll.VCI_ClearBuffer.restype = u
            self._dll.VCI_ReadBoardInfo.argtypes = [u, u, ctypes.POINTER(VCI_BOARD_INFO)]
            self._dll.VCI_ReadBoardInfo.restype = u
            return True
        except Exception as e:
            self._last_error = f"加载 ControlCAN.dll 失败: {e}"
            self._dll = None
            return False

    # ── CanTransport 接口 ──────────────────────────

    def connect(self, config: dict = None) -> bool:
        config = config or {}
        self._channel = int(config.get("channel", 0))
        self._baudrate = int(config.get("baudrate", 250000))

        if not self._load_dll():
            return False

        timing0, timing1 = BAUDRATE_MAP.get(self._baudrate, (0x01, 0x1C))
        try:
            # 1. 打开设备
            ret = self._dll.VCI_OpenDevice(self._dev_type, self._dev_index, 0)
            if ret != 1:
                self._last_error = "打开设备失败，请检查 CAN 盒连接和驱动"
                return False

            # 2. 初始化通道（接收所有类型扩展帧：AccCode/AccMask 全通、Filter=1、正常模式）
            cfg = VCI_INIT_CONFIG(0x80000008, 0xFFFFFFFF, 0, 1, timing0, timing1, 0)
            ret = self._dll.VCI_InitCAN(self._dev_type, self._dev_index, self._channel, ctypes.byref(cfg))
            if ret != 1:
                self._last_error = "初始化通道失败"
                self._dll.VCI_CloseDevice(self._dev_type, self._dev_index)
                return False

            # 3. 启动通道
            ret = self._dll.VCI_StartCAN(self._dev_type, self._dev_index, self._channel)
            if ret != 1:
                self._last_error = "启动通道失败"
                self._dll.VCI_CloseDevice(self._dev_type, self._dev_index)
                return False

            # 4. 启动接收线程
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
            if self._dll:
                self._dll.VCI_CloseDevice(self._dev_type, self._dev_index)
        except Exception:
            pass

    def reset(self) -> None:
        # 复位通道：清缓冲后重启，让充电机重新握手
        if not self._dll or not self._connected:
            return
        try:
            self._dll.VCI_ClearBuffer(self._dev_type, self._dev_index, self._channel)
            self._dll.VCI_ResetCAN(self._dev_type, self._dev_index, self._channel)
            self._dll.VCI_StartCAN(self._dev_type, self._dev_index, self._channel)
        except Exception:
            pass

    def send(self, frame: CanFrame) -> bool:
        if not self._connected or not self._dll:
            return False
        try:
            obj = VCI_CAN_OBJ()
            obj.ID = frame.can_id
            obj.TimeStamp = 0
            obj.TimeFlag = 0
            obj.SendType = 1            # 单次发送
            obj.RemoteFlag = 0          # 数据帧
            obj.ExternFlag = 1 if frame.is_extended else 0
            obj.DataLen = len(frame.data)
            for i, b in enumerate(frame.data):
                obj.Data[i] = b
            ret = self._dll.VCI_Transmit(
                self._dev_type, self._dev_index, self._channel, ctypes.byref(obj), 1)
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

    def register_disconnect_callback(self, cb) -> None:
        with self._callback_lock:
            if cb not in self._disconnect_cbs:
                self._disconnect_cbs.append(cb)

    def get_status(self) -> dict:
        return {
            "connected": self._connected,
            "device_type": "CANalyst-II",
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
        recv_buf = (VCI_CAN_OBJ * RECV_BUF_SIZE)()
        idle_ticks = 0
        while self._running:
            try:
                # 2ms 轮询（与 ZlgTransport 一致）
                rcv_num = self._dll.VCI_GetReceiveNum(
                    self._dev_type, self._dev_index, self._channel)
                if rcv_num <= 0:
                    time.sleep(0.002)
                    idle_ticks += 1
                    # 每约 500ms 检测一次设备在线，拔线后及时停线程、不再刷错误
                    if idle_ticks >= 250:
                        idle_ticks = 0
                        if not self._device_online():
                            self._handle_disconnect()
                            break
                    continue
                idle_ticks = 0
                read_num = min(rcv_num, RECV_BUF_SIZE)
                actual = self._dll.VCI_Receive(
                    self._dev_type, self._dev_index, self._channel,
                    recv_buf, read_num, 0)
                for i in range(actual):
                    obj = recv_buf[i]
                    can_id = obj.ID & 0x1FFFFFFF
                    dlc = obj.DataLen
                    data = bytes(obj.Data[:dlc]) if dlc > 0 else b""
                    cf = CanFrame(
                        can_id=can_id, data=data,
                        is_extended=bool(obj.ExternFlag),
                        timestamp=time.time(), direction="rx")
                    with self._lock:
                        self._rx_count += 1
                    self._notify(cf)
            except Exception:
                # 异常多半是设备掉线，检测后立即停止（避免持续刷错误）
                if not self._device_online():
                    self._handle_disconnect()
                    break
                time.sleep(0.005)

    def _device_online(self) -> bool:
        if not self._dll:
            return False
        try:
            info = VCI_BOARD_INFO()
            return self._dll.VCI_ReadBoardInfo(
                self._dev_type, self._dev_index, ctypes.byref(info)) == 1
        except Exception:
            return False

    def _handle_disconnect(self):
        """设备掉线：停线程、标记断开、通知上层"""
        self._running = False
        self._connected = False
        self._last_error = "CAN 设备已断开（掉线）"
        with self._callback_lock:
            cbs = list(self._disconnect_cbs)
        for cb in cbs:
            try:
                cb()
            except Exception:
                pass

    def _notify(self, frame: CanFrame):
        with self._callback_lock:
            cbs = list(self._callbacks)
        for cb in cbs:
            try:
                cb(frame)
            except Exception:
                pass
