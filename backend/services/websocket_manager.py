from fastapi import WebSocket
from typing import List
import json

class ConnectionManager:
    def __init__(self):
        # 这个列表用来存放所有连进来的前端网页（比如你的平板、电脑浏览器）
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        # 建立专线连接
        await websocket.accept()
        self.active_connections.append(websocket)
        print("✅ [广播站] 有新的前端指挥中心接入！")

    def disconnect(self, websocket: WebSocket):
        # 网页关掉时，断开连接
        self.active_connections.remove(websocket)
        print("❌ [广播站] 前端指挥中心已断开连接。")

    async def broadcast(self, message: dict):
        # 核心功能：用大喇叭把数据转成 JSON，群发给所有看着网页的人
        for connection in self.active_connections:
            await connection.send_text(json.dumps(message))

# 实例化一个全局的广播站大喇叭
manager = ConnectionManager()