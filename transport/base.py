"""CAN 传输抽象层"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable


@dataclass
class CanFrame:
    """一帧 CAN 报文。direction 从 BMS 模拟器视角定义：rx=收到、tx=发出"""
    can_id: int
    data: bytes
    is_extended: bool = True
    timestamp: float = 0.0
    direction: str = "rx"


class CanTransport(ABC):
    """CAN 传输抽象接口，zlgcan 与 mock 共用"""

    @abstractmethod
    def connect(self, config: dict) -> bool:
        """连接 CAN，config 含 device_type / channel / baudrate"""

    @abstractmethod
    def disconnect(self) -> None:
        """断开 CAN"""

    @abstractmethod
    def reset(self) -> None:
        """复位到初始状态，准备新一轮充电（Mock 重新发 CHM，真实 CAN 无操作）"""

    @abstractmethod
    def send(self, frame: CanFrame) -> bool:
        """发送一帧"""

    @abstractmethod
    def register_callback(self, cb: Callable[[CanFrame], None]) -> None:
        """注册接收回调（收到帧时调用）"""

    @abstractmethod
    def get_status(self) -> dict:
        """查询连接状态 + 设备信息 + 收发计数"""

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """是否已连接"""
