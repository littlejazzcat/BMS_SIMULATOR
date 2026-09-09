"""FastAPI 服务：装配各层 + REST 路由 + WebSocket"""

import asyncio
import csv
import io
import queue
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from protocol.models.loader import load_protocol
from protocol.services.can_codec import CanCodec
from transport.mock import MockTransport
from transport.zlg import ZlgTransport
from core.controller import ChargingController
from core.modes import create_mode, MODE_REGISTRY
from core.states import ChargingState
from server.ws import manager
from server.schemas import CanConnectRequest, ParamsUpdateRequest, ModeRequest
from server import storage
from paths import resource_path

# ── 全局装配对象（lifespan 里初始化） ──────────────
protocol = None
codec = None
private_protocol = None
transport = None
transport_mock = None
transport_zlg = None
controller = None
mode = None

# 事件队列：controller 线程 → asyncio 广播
event_queue: "queue.Queue[dict]" = queue.Queue()

PROTOCOL_PATH = resource_path("protocol/data/gbt27930_v1.1.json")


def event_cb(event_type: str, data: dict):
    """controller 事件回调（在状态机/transport 线程里调用），只入队"""
    event_queue.put({"type": event_type, "data": data})


async def event_pump():
    """节流批量推送：每 300ms drain 队列，帧事件打包批量广播，状态事件立即广播"""
    while True:
        await asyncio.sleep(0.3)
        frames_batch = []
        while True:
            try:
                event = event_queue.get_nowait()
            except queue.Empty:
                break
            etype = event.get("type")
            if etype in ("frame_tx", "frame_rx"):
                d = event["data"]
                ts = d.get("ts")
                time_str = (datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]
                            if ts else datetime.now().strftime("%H:%M:%S.%f")[:-3])
                rec = {
                    "direction": "tx" if etype == "frame_tx" else "rx",
                    "abbr": d.get("abbr"),
                    "can_id": d.get("can_id"),
                    "data_hex": d.get("data_hex"),
                    "time": time_str,
                    "parsed": d.get("parsed"),
                }
                frames_batch.append(rec)
            else:
                await manager.broadcast_state(event)
        if frames_batch:
            await asyncio.to_thread(storage.insert_messages, frames_batch)  # 写库丢线程池，不阻塞事件循环
            await manager.broadcast_messages({"type": "frames", "batch": frames_batch})


@asynccontextmanager
async def lifespan(app: FastAPI):
    global protocol, codec, private_protocol, transport, transport_mock, transport_zlg, controller, mode
    protocol = load_protocol(PROTOCOL_PATH)
    codec = CanCodec(protocol)
    try:
        private_protocol = load_protocol(resource_path("protocol/data/adaptive_private.json"))
    except Exception:
        private_protocol = None
    transport_mock = MockTransport(codec, charging_duration=8.0)
    transport_zlg = ZlgTransport()
    transport = transport_mock
    mode = create_mode("gb")
    controller = ChargingController(transport, codec, mode=mode, event_cb=event_cb)
    transport.register_callback(controller.on_frame)
    storage.init_db()
    pump_task = asyncio.create_task(event_pump())
    yield
    transport_mock.disconnect()
    transport_zlg.disconnect()
    controller.stop()
    pump_task.cancel()


app = FastAPI(title="BMS 模拟器", lifespan=lifespan)


# ── REST 路由 ────────────────────────────────────

@app.get("/api/status")
async def get_status():
    return {
        "state": controller.current_state.value if controller else "未初始化",
        "mode": mode.name if mode else "国标",
        "can_connected": transport.is_connected if transport else False,
        "tx_count": transport.get_status()["tx_count"] if transport else 0,
        "rx_count": transport.get_status()["rx_count"] if transport else 0,
        "error": controller._error_msg if controller else "",
    }


@app.get("/api/can/status")
async def get_can_status():
    return transport.get_status()


@app.post("/api/can/connect")
async def can_connect(req: CanConnectRequest):
    global transport
    transport.disconnect()
    new_transport = transport_mock if req.device_type == "Mock" else transport_zlg
    ok = new_transport.connect(req.model_dump())
    transport = new_transport  # 无论成败都指向用户选择的 transport，避免降级到 mock
    if ok:
        new_transport.register_callback(controller.on_frame)
        controller.transport = new_transport
    event_queue.put({
        "type": "can_connected" if ok else "can_connect_failed",
        "data": new_transport.get_status(),
    })
    return {"connected": ok, "status": new_transport.get_status()}


@app.post("/api/can/disconnect")
async def can_disconnect():
    transport.disconnect()
    event_queue.put({"type": "can_disconnected", "data": transport.get_status()})
    return {"connected": False, "status": transport.get_status()}


@app.get("/api/messages")
async def get_messages(offset: int = 0, limit: int = 200, max_id: int = None):
    """分页查询报文历史（从新到旧）。max_id 为快照锚点。"""
    messages, total, anchor = storage.query_messages(offset, limit, max_id)
    return {"messages": messages, "total": total, "max_id": anchor}


@app.get("/api/messages/export")
async def export_messages():
    """导出全部报文为 CSV（UTF-8 BOM，Excel 可直接打开）"""
    messages = storage.query_all_messages()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["方向", "报文", "CAN_ID", "数据HEX", "时间"])
    for m in messages:
        writer.writerow([
            m["direction"],
            m["abbr"],
            f"0x{m['can_id']:08X}" if m["can_id"] is not None else "",
            m["data_hex"],
            f'="{m["time"]}"',  # 强制文本，避免 Excel 吞掉毫秒
        ])
    csv_text = "\ufeff" + buf.getvalue()  # BOM，Excel 识别 UTF-8
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=bms_messages.csv"},
    )


@app.post("/api/messages/clear")
async def clear_messages():
    storage.clear_messages()
    return {"ok": True}


def _field_default(abbr: str, field) -> object:
    """字段默认值：优先用 controller.DEFAULT_PARAMS 的物理量，否则 enum 第一个值/0"""
    key = "BRO_AA" if abbr == "BRO" else abbr
    params = ChargingController.DEFAULT_PARAMS.get(key, {})
    if field.name in params:
        return params[field.name]
    if field.enum:
        return list(field.enum.values())[0]
    if field.data_type == "ascii":
        return ""
    return 0


@app.get("/api/protocol")
async def get_protocol():
    """返回所有 BMS 报文（车辆→充电机方向）的字段定义，给前端生成参数表单"""
    result = {}
    for msg in protocol.messages:
        if msg.direction != "车辆→充电机":
            continue
        cycle = 0 if msg.abbr in ChargingController.OPTIONAL_MESSAGES else msg.cycle_ms
        result[msg.abbr] = {
            "name": msg.name,
            "cycle_ms": cycle,
            "fields": [
                {
                    "name": f.name,
                    "unit": f.unit,
                    "resolution": f.resolution,
                    "offset": f.offset,
                    "enum": f.enum,
                    "data_type": f.data_type,
                    "default": _field_default(msg.abbr, f),
                }
                for f in msg.fields
            ],
        }
    # 自适应模式：额外返回 BFC 的枪数量设置
    if private_protocol and mode and mode.name == "自适应":
        bfc = private_protocol.get_message_by_abbr("BFC")
        if bfc:
            result["BFC"] = {
                "name": bfc.name,
                "cycle_ms": bfc.cycle_ms,
                "fields": [
                    {"name": "充电枪数量", "unit": "", "resolution": None, "offset": None,
                     "enum": None, "data_type": "int_le", "default": mode.gun_count},
                ],
            }
    return result


@app.post("/api/params")
async def update_params(req: ParamsUpdateRequest):
    controller.submit_command({"action": "update_params", "params": req.params})
    return {"ok": True}


@app.post("/api/start")
async def start_charging():
    if not transport.is_connected:
        return {"ok": False, "state": controller.current_state.value,
                "error": "请先连接 CAN"}
    transport.reset()
    controller.start()
    return {"ok": True, "state": controller.current_state.value}


@app.post("/api/stop")
async def stop_charging():
    controller.stop()
    return {"ok": True}


@app.get("/api/mode")
async def get_mode():
    return {"mode": mode.name, "available": list(MODE_REGISTRY.keys())}


@app.post("/api/mode")
async def set_mode(req: ModeRequest):
    global mode
    # 仅"等待CHM"或"已停止"状态允许切换模式
    if controller.is_charging and controller.current_state != ChargingState.WAITING_CHM:
        return {"ok": False, "mode": mode.name,
                "error": f"充电流程进行中（{controller.current_state.value}），请先停止充电再切换模式"}
    mode = create_mode(req.mode)
    if req.gun_count is not None:
        mode.gun_count = req.gun_count
    if req.fill_mode is not None:
        mode.fill_mode = req.fill_mode
    controller.mode = mode
    return {"ok": True, "mode": mode.name}


# ── WebSocket ────────────────────────────────────

@app.websocket("/ws/messages")
async def ws_messages(ws: WebSocket):
    await manager.connect_messages(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)


@app.websocket("/ws/state")
async def ws_state(ws: WebSocket):
    await manager.connect_state(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)


# 静态文件 + 首页（必须放最后，否则会拦截 /api 和 /ws 路由）
app.mount("/", StaticFiles(directory=resource_path("web"), html=True), name="static")
