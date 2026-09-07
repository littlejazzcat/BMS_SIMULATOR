"""pydantic 请求/响应模型"""

from typing import Any, Dict, Optional

from pydantic import BaseModel


class CanConnectRequest(BaseModel):
    device_type: str = "USBCAN-2E-U"
    channel: int = 0
    baudrate: int = 250000


class ParamsUpdateRequest(BaseModel):
    params: Dict[str, Dict[str, Any]]


class ModeRequest(BaseModel):
    mode: str = "gb"
    gun_count: Optional[int] = None
    fill_mode: Optional[str] = None


class StatusResponse(BaseModel):
    state: str
    mode: str
    can_connected: bool
    tx_count: int
    rx_count: int
    error: Optional[str] = None
