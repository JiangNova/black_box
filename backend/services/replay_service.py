"""
回放推流引擎 — Replay Streaming Engine

负责从 SQLite 逐帧读取指定 run_id 的历史遥测数据，按照原始 timestamp_us
差值等比还原时间间隔，通过 WebSocket 逐帧推送给前端，完整重现历史实验过程。

特性：
- 按原始时间戳差值精确 sleep，支持 1× / 2× / 5× / 10× 加速
- 前端接收帧格式与实时 MQTT broadcast 完全一致，无需额外适配
- 支持中途取消（用户切换回实时模式或断开连接）
- 全部帧发送完毕后自动通知前端「回放结束」
"""

import asyncio
import json
import logging
from typing import Set

from fastapi import WebSocket

from database.db_logger import db_logger
from services.websocket_manager import manager

logger = logging.getLogger("replay.service")

# 回放速度倍率映射：前端传 "1x" "2x" "5x" "10x"
SPEED_MAP = {
    "1x": 1.0,
    "2x": 2.0,
    "5x": 5.0,
    "10x": 10.0,
}
DEFAULT_SPEED = 1.0

# 最大帧间间隔（秒）—— 防止因数据断层导致过长时间无输出
MAX_FRAME_GAP_S = 2.0


class ReplayService:
    """
    回放推流服务。

    每个 WebSocket 客户端同时只能运行一个回放任务。
    _tasks 字典跟踪所有活跃的回放 asyncio.Task，支持按客户端取消。
    """

    def __init__(self):
        self._tasks: dict[int, asyncio.Task] = {}  # id(ws) → Task

    # ── 公开 API ──────────────────────────────────────────

    async def start_replay(
        self,
        websocket: WebSocket,
        run_id: int,
        speed: str = "1x",
    ) -> None:
        """
        启动回放推流。

        1. 校验 run_id 存在性
        2. 将客户端标记为 replay 模式
        3. 创建异步任务逐帧推流
        4. 若该客户端已有回放任务，先取消再启动
        """
        wid = id(websocket)

        # 若已有回放任务，先取消
        await self.stop_replay(websocket)

        if not await db_logger.run_exists(run_id):
            await manager.send_to(websocket, {
                "type": "replay_error",
                "run_id": run_id,
                "message": f"Run {run_id} 不存在",
            })
            logger.warning("回放请求的 run_id=%s 不存在", run_id)
            return

        speed_factor = SPEED_MAP.get(speed, DEFAULT_SPEED)

        # 标记客户端进入 replay 模式
        manager.set_mode(websocket, "replay", run_id)

        # 通知前端回放即将开始
        await manager.send_to(websocket, {
            "type": "replay_started",
            "run_id": run_id,
            "speed": speed,
        })

        # 创建后台任务
        task = asyncio.create_task(
            self._stream(websocket, run_id, speed_factor, wid),
            name=f"replay-run-{run_id}-ws-{wid}",
        )
        self._tasks[wid] = task

        logger.info(
            "🚀 回放开始: run_id=%s speed=%s (×%.1f) ws=%s",
            run_id, speed, speed_factor, wid,
        )

    async def stop_replay(self, websocket: WebSocket) -> None:
        """
        停止指定客户端的回放推流，恢复为 live 模式。
        若该客户端无活跃回放任务，则静默跳过。
        """
        wid = id(websocket)
        task = self._tasks.pop(wid, None)

        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            logger.info("⏹ 回放已取消: ws=%s", wid)

        # 恢复为 live 模式
        manager.set_mode(websocket, "live", None)

        # 通知前端回放已结束
        try:
            await manager.send_to(websocket, {
                "type": "replay_stopped",
            })
        except Exception:
            pass

    def is_replaying(self, websocket: WebSocket) -> bool:
        """检查指定客户端是否正在回放"""
        wid = id(websocket)
        task = self._tasks.get(wid)
        return task is not None and not task.done()

    # ── 内部推流逻辑 ──────────────────────────────────────

    async def _stream(
        self,
        websocket: WebSocket,
        run_id: int,
        speed_factor: float,
        wid: int,
    ) -> None:
        """
        逐帧推流协程。

        从 db_logger.get_replay_frames() 拉取每一帧，计算与上一帧的
        timestamp_us 差值 → 换算为秒 → 除以速度倍率 → asyncio.sleep，
        然后将帧通过 manager.send_to() 推送给指定客户端。
        """
        frame_count = 0
        prev_ts_us: int | None = None

        try:
            async for frame in db_logger.get_replay_frames(run_id):
                current_ts = frame["timestamp_us"]

                if prev_ts_us is not None:
                    delta_us = current_ts - prev_ts_us
                    if delta_us > 0:
                        delta_s = min(delta_us / 1_000_000.0, MAX_FRAME_GAP_S) / speed_factor
                        await asyncio.sleep(delta_s)

                # 推帧
                await manager.send_to(websocket, frame)
                frame_count += 1
                prev_ts_us = current_ts

            # 全部帧发送完毕
            await manager.send_to(websocket, {
                "type": "replay_complete",
                "run_id": run_id,
                "total_frames": frame_count,
            })
            logger.info(
                "✅ 回放完成: run_id=%s total_frames=%s ws=%s",
                run_id, frame_count, wid,
            )

        except asyncio.CancelledError:
            logger.info("🛑 回放任务被取消: run_id=%s frames=%s", run_id, frame_count)
            raise

        except Exception:
            logger.exception("回放推流异常: run_id=%s", run_id)
            try:
                await manager.send_to(websocket, {
                    "type": "replay_error",
                    "run_id": run_id,
                    "message": "回放推流内部错误",
                })
            except Exception:
                pass

        finally:
            # 清理并恢复 live 模式
            self._tasks.pop(wid, None)
            manager.set_mode(websocket, "live", None)


# 全局单例
replay_service = ReplayService()
