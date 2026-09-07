"""充电状态枚举（照搬 EV 参考实现的 11 状态）"""

from enum import Enum


class ChargingState(Enum):
    WAITING_CHM = "等待CHM报文"
    SENDING_BHM = "发送BHM报文"
    SENDING_BRM = "发送BRM报文"
    SENDING_BCP = "发送BCP报文"
    SENDING_BRO_00 = "发送BRO=00"
    SENDING_BRO_AA = "发送BRO=AA"
    CHARGING = "充电中"
    SENDING_BST = "发送BST报文"
    SENDING_BSD = "发送BSD报文"
    ERROR = "错误状态"
    COMPLETED = "充电完成"
