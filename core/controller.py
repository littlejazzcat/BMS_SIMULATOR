"""充电状态机：BMS 侧。

保留 EV 参考实现的状态机骨架，组包统一改用 codec.encode。
状态转换由「收到充电机报文」驱动，周期发送由 PeriodTimer 驱动。
"""

import logging
import queue
import threading
import time

from core.states import ChargingState
from core.modes import ChargingMode, GbMode, AdaptiveMode
from protocol.models.loader import load_protocol
from protocol.services.can_codec import CanCodec as ProtocolCodec
from transport.base import CanFrame
from paths import resource_path

logger = logging.getLogger(__name__)


class ChargingController:
    """BMS 侧充电状态机"""

    # 可选报文（默认周期 0，不发送）
    OPTIONAL_MESSAGES = {"BMV", "BMT", "BSP"}

    # 各报文的默认物理量参数（demo 用）
    DEFAULT_PARAMS = {
        "BHM": {"绝缘监测允许总电压": 750.0},
        "BRM": {
            "车辆通信协议版本号": 0x000101,       # V1.1 → 01 01 00
            "电池类型": "磷酸铁锂",
            "额定容量": 180.0,                    # Ah
            "额定总电压": 750.0,                  # V
            "电池生产厂商名称": "SAIT",           # 4 字节 ascii（SAITER 前 4 字符）
            "生产日期-年": 2025,                  # 物理量年份，编码 = 年份 - 1985
            "生产日期-月": 1,
            "生产日期-日": 1,
            "扩展车辆识别码(EVIN)": "SAITER00000000001",  # 17 字节 ascii
        },
        "BCP": {
            "单体蓄电池最高允许充电电压": 4.2,
            "最高允许充电电流": 180.0,
            "动力蓄电池标称总能量": 135.0,
            "最高允许充电总电压": 750.0,
            "单体蓄电池最高允许温度": 55.0,
            "整车荷电状态SOC": 50.0,
            "车辆接口当前电压测量值": 750.0,
        },
        "BRO_00": {"车辆充电准备好": "未准备好"},
        "BRO_AA": {"车辆充电准备好": "准备好(0xAA)"},
        "BCL": {
            "车辆接口充电电压需求": 400.0,
            "车辆接口充电电流需求": -180.0,
            "充电模式": "恒流充电",
        },
        "BCS": {
            "车辆接口当前电压测量值": 400.0,
            "车辆接口当前电流测量值": -180.0,
            "最高单体蓄电池电压及其组号": (420 << 4) | 1,  # 4.2V + 组号1
            "当前荷电状态SOC": 50.0,
            "估算剩余充电时间": 30.0,
        },
        "BSM": {
            "最高单体蓄电池电压所在编号": 1,
            "当前单体蓄电池最高温度": 30.0,
            "最高动力蓄电池温度检测点编号": 1,
            "当前单体蓄电池最低温度": 25.0,
            "最低动力蓄电池温度检测点编号": 1,
            "单体蓄电池电压过高/过低": "正常",
            "动力蓄电池荷电状态SOC过高/过低": "正常",
            "动力蓄电池充电过电流": "正常",
            "动力蓄电池温度过高": "正常",
            "动力蓄电池绝缘状态": "正常",
            "动力蓄电池组输出连接器连接状态": "正常",
            "充电允许": "允许(恢复)",
        },
        "BST": {"主动中止": "充电机中止"},
        "BSD": {
            "中止荷电状态SOC": 80.0,
            "单体蓄电池最低电压": 3.2,
            "单体蓄电池最高电压": 4.2,
            "动力蓄电池最低温度": 25.0,
            "动力蓄电池最高温度": 30.0,
        },
        "BEM": {"接收CTS/CML超时": "超时"},
    }

    # 超时配置（秒）
    TIMEOUTS = {
        "wait_chm": 30.0,       # 等 CHM
        "wait_crm_00": 30.0,    # 发 BHM 后等 CRM=00
        "wait_crm_aa": 5.0,     # 发 BRM 后等 CRM=AA
        "wait_cml": 5.0,        # 发 BCP 后等 CML
        "wait_cro_aa": 60.0,    # 发 BRO=AA 后等 CRO=AA
    }

    # 参数 key → 报文缩写的映射（BRO 用数据域首字节区分 00/AA）
    _ABBR_MAP = {"BRO_00": "BRO", "BRO_AA": "BRO"}

    def __init__(self, transport, codec, mode: ChargingMode = None,
                 params: dict = None, event_cb=None):
        self.transport = transport
        self.codec = codec
        self.protocol = codec.protocol
        self.mode = mode or GbMode()
        self.event_cb = event_cb

        # BMS 参数（物理量），默认用 DEFAULT_PARAMS，可被 params 覆盖
        self.params = {}
        self.params.update(self.DEFAULT_PARAMS)
        if params:
            for k, v in params.items():
                self.params.setdefault(k, {}).update(v)

        self.current_state = ChargingState.WAITING_CHM
        self.is_charging = False
        self._stop_requested = False
        self._error_msg = ""

        self._thread = None
        self._lock = threading.Lock()
        self._command_queue = queue.Queue()

        # 预计算各报文 CAN ID
        self._can_ids = {}
        for m in self.protocol.messages:
            self._can_ids[m.abbr] = self.codec.make_can_id(m)

        # 私有协议（CFC/BFC），自适应模式用
        self.private_protocol = None
        self.private_codec = None
        try:
            self.private_protocol = load_protocol(resource_path("protocol/data/adaptive_private.json"))
            self.private_codec = ProtocolCodec(self.private_protocol)
            for m in self.private_protocol.messages:
                self._can_ids[m.abbr] = self.private_codec.make_can_id(m)
        except Exception as e:
            logger.error("加载私有协议失败: %s", e)

        # 发送周期配置（ms），0 = 不发送；从 protocol 初始化默认周期，可选报文默认 0
        self._cycle_ms = {}
        for m in self.protocol.messages:
            if m.direction == "车辆→充电机":
                self._cycle_ms[m.abbr] = 0 if m.abbr in self.OPTIONAL_MESSAGES else m.cycle_ms
        if self.private_protocol:
            for m in self.private_protocol.messages:
                if m.direction == "车辆→充电机":
                    self._cycle_ms[m.abbr] = m.cycle_ms
        self._last_sent = {}

        # 状态进入时间、各报文首次发送时间
        self._state_start = 0.0
        self._first_sent = {}

        # 收到报文的缓冲：can_id -> 最近一次 data
        self._received = {}

    # ── 生命周期 ──────────────────────────────────

    def start(self):
        if self.is_charging:
            return
        self.is_charging = True
        self._stop_requested = False
        self.current_state = ChargingState.WAITING_CHM
        self._state_start = time.time()
        self._first_sent = {}
        self._received = {}
        self._emit("state_changed", {"state": self.current_state.value})
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        """请求停止（BMS 主动中止，仅充电态生效）"""
        self._stop_requested = True

    def on_frame(self, frame: CanFrame):
        """transport 回调：收到充电机报文"""
        with self._lock:
            self._received[frame.can_id] = frame.data
        msg = self.codec.identify_message(frame.can_id, frame.data)

        # CFC 处理（自适应模式）
        is_cfc = bool(self.private_protocol) and frame.can_id == self._can_ids.get("CFC")
        if is_cfc:
            self.mode.on_cfc(self, frame.data)
        # CRM 处理（FAST 模式检查标识）
        elif msg is not None and msg.abbr == "CRM":
            self.mode.on_crm(self, frame.data)

        # 解析数据域（多帧不解析）
        if is_cfc:
            cfc_msg = self.private_protocol.get_message_by_abbr("CFC")
            parsed = self._parse_fields(self.private_codec, cfc_msg, frame.data)
        else:
            parsed = self._parse_fields(self.codec, msg, frame.data)

        self._emit("frame_rx", {
            "can_id": frame.can_id,
            "data_hex": frame.data.hex(" ").upper(),
            "abbr": "CFC" if is_cfc else (msg.abbr if msg else None),
            "ts": frame.timestamp if frame.timestamp > 0 else time.time(),
            "parsed": parsed,
        })

    def submit_command(self, cmd: dict):
        """web 控制指令入口"""
        self._command_queue.put(cmd)

    # ── 状态机主循环 ──────────────────────────────

    def _run(self):
        while self.is_charging:
            self._drain_commands()
            if self._stop_requested:
                if self.current_state == ChargingState.CHARGING:
                    self._stop_requested = False  # 复位，让 BST/BSD 流程走完
                    self._transition(ChargingState.SENDING_BST)
                else:
                    self.is_charging = False
                    break
            try:
                # 自适应模式：BFC 周期发送
                if isinstance(self.mode, AdaptiveMode) and self._should_send("BFC"):
                    self._send_bfc()
                self._dispatch()
            except Exception as e:
                logger.error("状态机异常: %s", e)
            time.sleep(0.005)  # 5ms 轮询
        self._emit("state_changed", {"state": "已停止"})

    def _drain_commands(self):
        while True:
            try:
                cmd = self._command_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_command(cmd)

    def _handle_command(self, cmd: dict):
        action = cmd.get("action")
        if action == "update_params":
            for abbr, fields in cmd.get("params", {}).items():
                key = abbr.upper()
                # BFC 枪数量 → mode.gun_count（自适应模式）
                if key == "BFC" and "充电枪数量" in fields:
                    self.mode.gun_count = int(fields["充电枪数量"])
                    continue
                if key not in self.params:
                    continue
                cycle = fields.get("__cycle_ms__")
                if cycle is not None:
                    self._cycle_ms[key] = int(cycle)
                phys = {k: v for k, v in fields.items() if k != "__cycle_ms__"}
                self.params[key].update(phys)
            self._emit("param_applied", {"params": cmd.get("params", {})})
        elif action == "stop":
            self._stop_requested = True

    def _dispatch(self):
        st = self.current_state
        if st == ChargingState.WAITING_CHM:
            self._state_waiting_chm()
        elif st == ChargingState.SENDING_BHM:
            self._state_sending_bhm()
        elif st == ChargingState.SENDING_BRM:
            self._state_sending_brm()
        elif st == ChargingState.SENDING_BCP:
            self._state_sending_bcp()
        elif st == ChargingState.SENDING_BRO_00:
            self._state_sending_bro_00()
        elif st == ChargingState.SENDING_BRO_AA:
            self._state_sending_bro_aa()
        elif st == ChargingState.CHARGING:
            self._state_charging()
        elif st == ChargingState.SENDING_BST:
            self._state_sending_bst()
        elif st == ChargingState.SENDING_BSD:
            self._state_sending_bsd()
        elif st == ChargingState.ERROR:
            self._state_error()
        elif st == ChargingState.COMPLETED:
            self.is_charging = False

    # ── 各状态处理 ────────────────────────────────

    def _state_waiting_chm(self):
        if self._got("CHM"):
            self._transition(ChargingState.SENDING_BHM)
            return
        if self._elapsed() > self.TIMEOUTS["wait_chm"]:
            self._enter_error("等待CHM超时")

    def _state_sending_bhm(self):
        if "BHM" not in self._first_sent:
            self._send("BHM", self.params["BHM"])
            self._first_sent["BHM"] = time.time()
            return
        if self._got_first("CRM", 0x00):
            self._transition(ChargingState.SENDING_BRM)
            return
        if self._should_send("BHM"):
            self._send("BHM", self.params["BHM"])
        if time.time() - self._first_sent["BHM"] > self.TIMEOUTS["wait_crm_00"]:
            self._enter_error("等待CRM=00超时")

    def _state_sending_brm(self):
        if "BRM" not in self._first_sent:
            self._send("BRM", self.params["BRM"])
            self._first_sent["BRM"] = time.time()
            return
        # 收到 CRM=AA（标准/国标）或 CRM=EE（FAST 大电流确认）→ 停发 BRM，转 BCP
        if self._got_first("CRM", 0xAA) or self._got_first("CRM", 0xEE):
            self._transition(ChargingState.SENDING_BCP)
            return
        if self._should_send("BRM"):  # 按设定周期（250ms）重发 BRM
            self._send("BRM", self.params["BRM"])
        if time.time() - self._first_sent["BRM"] > self.TIMEOUTS["wait_crm_aa"]:
            self._enter_error("等待CRM=AA超时")

    def _state_sending_bcp(self):
        if "BCP" not in self._first_sent:
            self._send("BCP", self.params["BCP"])
            self._first_sent["BCP"] = time.time()
            return
        if self._got("CML"):
            self._transition(ChargingState.SENDING_BRO_00)
            return
        if time.time() - self._first_sent["BCP"] > self.TIMEOUTS["wait_cml"]:
            self._enter_error("等待CML超时")

    def _state_sending_bro_00(self):
        # 发 3 帧 BRO=00 后转 BRO=AA（简化：用计数器）
        count = self._first_sent.get("BRO_00_count", 0)
        if count < 3:
            self._send("BRO_00", self.params["BRO_00"])
            self._first_sent["BRO_00_count"] = count + 1
            return
        self._transition(ChargingState.SENDING_BRO_AA)

    def _state_sending_bro_aa(self):
        if "BRO_AA" not in self._first_sent:
            self._send("BRO_AA", self.params["BRO_AA"])
            self._first_sent["BRO_AA"] = time.time()
            return
        if self._got_first("CRO", 0xAA):
            self._transition(ChargingState.CHARGING)
            return
        if self._should_send("BRO"):
            self._send("BRO_AA", self.params["BRO_AA"])
        if time.time() - self._first_sent["BRO_AA"] > self.TIMEOUTS["wait_cro_aa"]:
            self._enter_error("等待CRO=AA超时")

    def _state_charging(self):
        if self._got("CST"):
            self._transition(ChargingState.SENDING_BST)
            return
        if self._should_send("BCL"):
            self._send("BCL", self.params["BCL"])
        if self._should_send("BCS"):
            self._send("BCS", self.params["BCS"])
        if self._should_send("BSM"):
            self._send("BSM", self.params["BSM"])

    def _state_sending_bst(self):
        count = self._first_sent.get("BST_count", 0)
        if count < 10:
            self._send("BST", self.params["BST"])
            self._first_sent["BST_count"] = count + 1
            return
        self._transition(ChargingState.SENDING_BSD)

    def _state_sending_bsd(self):
        if "BSD" not in self._first_sent:
            self._send("BSD", self.params["BSD"])
            self._first_sent["BSD"] = time.time()
            return
        if self._should_send("BSD"):
            self._send("BSD", self.params["BSD"])
        if time.time() - self._first_sent["BSD"] > 5.0:
            self._transition(ChargingState.COMPLETED)

    def _state_error(self):
        if "BEM" not in self._first_sent:
            self._send("BEM", self.params["BEM"])
            self._first_sent["BEM"] = time.time()
            return
        # 收到 CRM=00 重新握手
        if self._got_first("CRM", 0x00):
            self._transition(ChargingState.SENDING_BRM)
            return
        if self._should_send("BEM"):
            self._send("BEM", self.params["BEM"])
        if time.time() - self._first_sent["BEM"] > 30.0:
            self._transition(ChargingState.COMPLETED)

    # ── 辅助 ──────────────────────────────────────

    def _should_send(self, abbr: str) -> bool:
        """按配置周期判断是否该发送（周期 0 = 不发送）"""
        cycle = self._cycle_ms.get(abbr, 0)
        if cycle <= 0:
            return False
        now = time.monotonic()
        last = self._last_sent.get(abbr, 0.0)
        if now - last >= cycle / 1000.0:
            self._last_sent[abbr] = now
            return True
        return False

    def _got(self, abbr: str) -> bool:
        with self._lock:
            return self._can_ids[abbr] in self._received

    def _got_first(self, abbr: str, first_byte: int) -> bool:
        with self._lock:
            data = self._received.get(self._can_ids[abbr])
        return bool(data and len(data) > 0 and data[0] == first_byte)

    def _elapsed(self) -> float:
        return time.time() - self._state_start

    def _transition(self, new_state: ChargingState):
        self.current_state = new_state
        self._state_start = time.time()
        self._emit("state_changed", {"state": new_state.value})

    def _enter_error(self, msg: str):
        self._error_msg = msg
        self._emit("error", {"message": msg})
        self._transition(ChargingState.ERROR)

    def _send(self, abbr: str, params: dict):
        """编码并发送一帧（或一组 TP 帧）BMS 报文"""
        msg_abbr = self._ABBR_MAP.get(abbr, abbr)
        msg = self.protocol.get_message_by_abbr(msg_abbr)
        if msg is None:
            logger.error("未找到报文: %s", msg_abbr)
            return

        # 电流字段 offset 覆盖 + 有符号编码（按模式）
        field_overrides = {}
        for f in msg.fields:
            if f.offset is not None:
                offset = self.mode.get_offset(f.name)
                if offset is not None:
                    field_overrides[f.name] = {"offset": offset, "signed": self.mode.is_signed()}

        r = self.codec.encode(msg, params, field_overrides)

        # BRM 编码后改字节（FAST 填充标识）
        if msg_abbr == "BRM" and msg.tp:
            data = bytearray(r["data"])
            data = self.mode.post_encode_brm(data)
            r["data"] = bytes(data)
            r["data_hex"] = bytes(data).hex(" ").upper()
            r["tp_frames"] = self.codec.tp_split(msg, bytes(data), r["can_id"])

        if r.get("tp") and r.get("tp_frames"):
            # TP 分帧：每帧单独发送 + 单独 emit，前端/导出按分帧显示
            for fr in r["tp_frames"]:
                if "CTS" in fr.get("type", ""):
                    continue  # CTS 是接收方（充电机）回的流控帧，BMS 不发
                frame = CanFrame(can_id=fr["can_id"], data=fr["data"], direction="tx")
                self.transport.send(frame)
                self._emit("frame_tx", {
                    "abbr": msg_abbr,
                    "can_id": frame.can_id,
                    "data_hex": frame.data.hex(" ").upper(),
                    "tp": True,
                    "ts": time.time(),
                    "parsed": None,  # TP 分片不解析
                })
        else:
            frame = CanFrame(can_id=r["can_id"], data=r["data"], direction="tx")
            self.transport.send(frame)
            self._emit("frame_tx", {
                "abbr": msg_abbr,
                "can_id": r["can_id"],
                "data_hex": r["data_hex"],
                "tp": False,
                "ts": time.time(),
                "parsed": self._parse_fields(self.codec, msg, r["data"]),
            })

    def _send_bfc(self):
        """自适应模式：周期发送 BFC 报文"""
        if not self.private_codec:
            return
        mode = self.mode
        # 应答信号
        if not mode.cfc_received:
            response = "未收到CFC"
        elif mode.charger_gun_count == mode.gun_count:
            response = "枪数量一致"
        else:
            response = "枪数量不一致"
        # 偏移量：回传 CFC 的值（编码值 = -offset / 100）
        offset_byte = int(-mode.current_offset / 100)
        bfc_msg = self.private_protocol.get_message_by_abbr("BFC")
        r = self.private_codec.encode(bfc_msg, {
            "电流偏移量": offset_byte,
            "充电枪数量": mode.gun_count,
            "应答信号": response,
        })
        frame = CanFrame(can_id=r["can_id"], data=r["data"], direction="tx")
        self.transport.send(frame)
        self._emit("frame_tx", {
            "abbr": "BFC",
            "can_id": r["can_id"],
            "data_hex": r["data_hex"],
            "tp": False,
            "ts": time.time(),
            "parsed": self._parse_fields(self.private_codec, bfc_msg, r["data"]),
        })

    @staticmethod
    def _parse_fields(codec, msg, data):
        """解析单帧报文数据域 → [{name, value}]。多帧或无 msg 返回 None。"""
        if msg is None:
            return None
        if getattr(msg, "tp", False) or (msg.data_length or 0) > 8:
            return None
        try:
            result = codec.decode(msg, data)
        except Exception:
            return None
        parsed = []
        for f in msg.fields:
            v = result.get(f.name, "N/A")
            if isinstance(v, dict):
                label = v.get("label")
                s = str(label) if label is not None else str(v.get("value"))
            else:
                s = str(v)
            if f.unit and not isinstance(v, dict):
                s = f"{s} {f.unit}"
            parsed.append({"name": f.name, "value": s})
        return parsed

    def _emit(self, event_type: str, data: dict):
        if self.event_cb:
            try:
                self.event_cb(event_type, data)
            except Exception:
                pass
