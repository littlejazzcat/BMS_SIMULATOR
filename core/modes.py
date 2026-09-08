"""三种充电模式策略：国标 / FAST / 自适应。

模式差异收敛到这里：电流 offset、有符号编码、CRM/CFC 处理、BRM 标识填充。
模式实例持有自身状态（枪数量、偏移量、FAST 填充方式等），不散落在 controller。
"""


class ChargingMode:
    """模式基类（国标）"""

    name = "国标"

    def __init__(self):
        self.gun_count = 2              # 自适应用：BMS 检测到的有效插枪数量
        self.current_offset = -400.0    # 当前电流偏移量（负值）

    # ── 编码相关 ──
    def get_offset(self, field_name: str):
        """电流字段的 offset 覆盖，None 表示用 JSON 默认值"""
        return None

    def is_signed(self) -> bool:
        """是否用有符号整数编码电流（负数不 clamp）"""
        return False

    # ── 报文处理 ──
    def on_crm(self, controller, crm_data: bytes):
        """收到 CRM 后的模式处理，返回动作描述或 None"""
        return None

    def on_cfc(self, controller, cfc_data: bytes):
        """收到 CFC 后的处理（自适应），返回解析结果或 None"""
        return None

    def post_encode_brm(self, data: bytearray) -> bytearray:
        """BRM 编码后改字节（FAST 填充标识用）"""
        return data

    # ── 配置入口 ──
    def set_param(self, key: str, value):
        setattr(self, key, value)


class GbMode(ChargingMode):
    """国标：全按 JSON 原值（电流 offset=-400）"""
    name = "国标"


class FastMode(ChargingMode):
    """FAST 大电流协议：电流无偏移有符号；CRM 需含 FAST 标识；BRM 按选项填充标识"""

    name = "FAST"
    FILL_OPTIONS = ("none", "fast", "ee", "fast_ee")

    def __init__(self):
        super().__init__()
        self.fill_mode = "fast"         # none / fast / ee / fast_ee
        self.fast_verified = False      # CRM 是否检测到 FAST 标识
        self.fast_confirmed = False     # CRM=EE 是否确认大电流
        self.fallback_to_gb = False     # CRM=AA 是否回退国标

    def get_offset(self, field_name: str):
        if "电流" in field_name:
            if self.fallback_to_gb:
                return None  # 回退国标：用 JSON 默认 -400 偏移（无符号）
            return 0  # FAST 取消 -400 偏移（有符号编码）
        return None  # 非电流字段用 JSON 默认值

    def is_signed(self) -> bool:
        return True  # 有符号编码（充电为负）

    def on_crm(self, controller, crm_data: bytes):
        # CRM 数据域 byte2~5 是否为 "FAST"（46 41 53 54）
        if len(crm_data) >= 5 and crm_data[1:5] == b'\x46\x41\x53\x54':
            self.fast_verified = True
        # CRM byte0：AA=回退国标，EE=确认大电流
        if len(crm_data) > 0:
            if crm_data[0] == 0xAA:
                self.fallback_to_gb = True
            elif crm_data[0] == 0xEE:
                self.fast_confirmed = True
        return None

    def post_encode_brm(self, data: bytearray) -> bytearray:
        # spn2570（电池组序号）byte13~16 → "FAST"
        if self.fill_mode in ("fast", "fast_ee") and len(data) >= 16:
            data[12:16] = b'\x46\x41\x53\x54'
        # spn2574（预留）byte24 → 0xEE
        if self.fill_mode in ("ee", "fast_ee") and len(data) >= 24:
            data[23] = 0xEE
        return data


class AdaptiveMode(ChargingMode):
    """自适应：电流偏移量随 CFC 下发值变化；BFC 周期回复，枪数量比对不停止"""

    name = "自适应"

    def __init__(self):
        super().__init__()
        self.cfc_received = False
        self.charger_gun_count = None

    def get_offset(self, field_name: str):
        if "电流" in field_name:
            return self.current_offset  # CFC 下发的电流偏移量
        return None  # 非电流字段用 JSON 默认值

    def on_cfc(self, controller, cfc_data: bytes):
        # CFC: byte1=电流偏移值(正值, 100A/bit), byte2 低4位=充电机枪数量
        if len(cfc_data) < 2:
            return None
        offset_byte = cfc_data[0]
        self.current_offset = -offset_byte * 100  # 负偏移
        self.charger_gun_count = cfc_data[1] & 0x0F
        self.cfc_received = True
        return {
            "offset": self.current_offset,
            "charger_gun_count": self.charger_gun_count,
            "match": self.charger_gun_count == self.gun_count,
        }


MODE_REGISTRY = {
    "gb": GbMode,
    "fast": FastMode,
    "adaptive": AdaptiveMode,
}


def create_mode(mode_key: str) -> ChargingMode:
    cls = MODE_REGISTRY.get(mode_key, GbMode)
    return cls()
