from fastapi import WebSocket
from typing import List, Dict
import json
import logging

logger = logging.getLogger("ws.manager")


class ConnectionManager:
    """
    WebSocket 连接管理器 —— 支持每客户端模式隔离：

    - "live" 模式（默认）：接收 MQTT 实时 broadcast
    - "replay" 模式：被 broadcast 跳过，由 replay_service 独占推流
    """

    def __init__(self):
        self.active_connections: List[WebSocket] = []
        # 每客户端元数据：{id(ws): {"mode": "live"|"replay", "run_id": int|None}}
        self._meta: Dict[int, dict] = {}

    # ── 连接生命周期 ──────────────────────────────────────

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        self._meta[id(websocket)] = {"mode": "live", "run_id": None}
        logger.info("✅ 前端连接加入，当前在线: %s", len(self.active_connections))

    def disconnect(self, websocket: WebSocket):
        wid = id(websocket)
        self.active_connections[:] = [c for c in self.active_connections if id(c) != wid]
        self._meta.pop(wid, None)
        logger.info("❌ 前端连接断开，当前在线: %s", len(self.active_connections))

    # ── 模式切换 ──────────────────────────────────────────

    def set_mode(self, websocket: WebSocket, mode: str, run_id: int | None = None):
        """将指定客户端切换为 live 或 replay 模式"""
        wid = id(websocket)
        if wid in self._meta:
            self._meta[wid]["mode"] = mode
            self._meta[wid]["run_id"] = run_id
            logger.info("🔄 客户端 %s 模式 → %s (run_id=%s)", wid, mode, run_id)

    def get_mode(self, websocket: WebSocket) -> str:
        """获取客户端当前模式"""
        wid = id(websocket)
        meta = self._meta.get(wid, {})
        return meta.get("mode", "live")

    def get_replay_run_id(self, websocket: WebSocket) -> int | None:
        """获取客户端当前回放的 run_id"""
        wid = id(websocket)
        meta = self._meta.get(wid, {})
        return meta.get("run_id")

    # ── 消息发送 ──────────────────────────────────────────

    async def broadcast(self, message: dict):
        """
        向所有 **live 模式** 客户端广播 MQTT 实时帧。
        处于 replay 模式的客户端由 replay_service 独立推流，此处跳过。
        """
        payload = json.dumps(message)
        for connection in self.active_connections:
            if self._meta.get(id(connection), {}).get("mode") == "replay":
                continue
            try:
                await connection.send_text(payload)
            except Exception:
                logger.warning("发送实时帧失败，客户端可能已断开")

    async def send_to(self, websocket: WebSocket, message: dict):
        """向指定客户端发送消息（用于回放独占推流或控制信令）"""
        try:
            await websocket.send_text(json.dumps(message))
        except Exception:
            logger.warning("向单个客户端发送消息失败")


# 全局单例
manager = ConnectionManager()
