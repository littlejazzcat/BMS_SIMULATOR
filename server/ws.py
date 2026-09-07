"""WebSocket 连接管理：/ws/messages 推收发帧，/ws/state 推状态事件"""

from fastapi import WebSocket


class ConnectionManager:
    def __init__(self):
        self.messages_conns = set()
        self.state_conns = set()

    async def connect_messages(self, ws: WebSocket):
        await ws.accept()
        self.messages_conns.add(ws)

    async def connect_state(self, ws: WebSocket):
        await ws.accept()
        self.state_conns.add(ws)

    def disconnect(self, ws: WebSocket):
        self.messages_conns.discard(ws)
        self.state_conns.discard(ws)

    async def broadcast_messages(self, message: dict):
        for ws in list(self.messages_conns):
            try:
                await ws.send_json(message)
            except Exception:
                self.messages_conns.discard(ws)

    async def broadcast_state(self, message: dict):
        for ws in list(self.state_conns):
            try:
                await ws.send_json(message)
            except Exception:
                self.state_conns.discard(ws)


manager = ConnectionManager()
