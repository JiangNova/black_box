import csv
import io
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from database.db_logger import db_logger
from hardware.mqtt_bridge import mqtt_bridge
from services.control_mux import mux_commander
from services.replay_service import replay_service
from services.websocket_manager import manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db_logger.init_db()
    await db_logger.start_new_run()
    await mqtt_bridge.start()
    logger.info("Black box backend started")
    try:
        yield
    finally:
        await mqtt_bridge.stop()
        logger.info("Black box backend shutdown")


app = FastAPI(lifespan=lifespan)


@app.get("/api/runs")
async def list_runs():
    """返回所有实验批次的简要信息，按时间倒序。"""
    try:
        runs = await db_logger.get_all_runs()
        return {"runs": runs, "total": len(runs)}
    except Exception:
        logger.exception("Failed to list runs")
        raise HTTPException(status_code=500, detail="Failed to query runs")


@app.get("/api/sessions")
async def list_sessions():
    """
    返回所有历史实验批次（会话），供前端复盘下拉菜单使用。
    与 /api/runs 等价，按 start_time 倒序排列。
    """
    try:
        runs = await db_logger.get_all_runs()
        sessions = [
            {
                "session_id": r["run_id"],
                "start_time": r["start_time"],
                "note": r.get("note", ""),
                "run_mode": r.get("run_mode", "—"),
                "sample_count": r.get("sample_count", 0),
            }
            for r in runs
        ]
        return {"sessions": sessions, "total": len(sessions)}
    except Exception:
        logger.exception("Failed to list sessions")
        raise HTTPException(status_code=500, detail="Failed to query sessions")


@app.get("/api/runs/{run_id}/telemetry")
async def get_run_telemetry(run_id: int):
    """返回指定批次的列式遥测数据，供历史复盘与图表渲染。"""
    if run_id <= 0:
        raise HTTPException(status_code=400, detail="run_id must be a positive integer")

    try:
        telemetry = await db_logger.get_telemetry_by_run(run_id)
    except Exception:
        logger.exception("Failed to query telemetry for run_id=%s", run_id)
        raise HTTPException(status_code=500, detail="Failed to query telemetry")

    if telemetry is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    return telemetry


CSV_HEADER = [
    "timestamp",
    "run_mode",
    "chassis_speed_mps",
    "chassis_steer_angle_deg",
    "chassis_gyro_z_rads",
    "chassis_yaw",
    "chassis_pitch",
    "chassis_roll",
    "chassis_aeb_active",
    "lidar_front_m",
    "vision_has_obstacle",
    "vision_obstacle_label",
    "vision_distance_m",
]


@app.get("/api/runs/{run_id}/export")
async def export_run_csv(run_id: int):
    """流式导出指定批次的遥测数据为 CSV 文件，边查边写，避免内存膨胀。"""
    if run_id <= 0:
        raise HTTPException(status_code=400, detail="run_id must be a positive integer")

    if not await db_logger.run_exists(run_id):
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    async def csv_generator():
        buf = io.StringIO()
        writer = csv.writer(buf)

        # 表头
        writer.writerow(CSV_HEADER)
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)

        # 数据行 — 逐行 yield，绝不缓存全部数据
        async for row in db_logger.export_telemetry_rows(run_id):
            writer.writerow([row[h] for h in CSV_HEADER])
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)

    return StreamingResponse(
        csv_generator(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=chassis_telemetry_run_{run_id}.csv"
        },
    )


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type", "")

            # ── 回放控制 ──────────────────────────────────
            if msg_type == "replay_start":
                run_id = msg.get("run_id")
                if run_id is None:
                    await manager.send_to(websocket, {
                        "type": "replay_error",
                        "message": "缺少 run_id 参数",
                    })
                    continue

                speed = msg.get("speed", "1x")
                await replay_service.start_replay(websocket, int(run_id), speed)
                continue

            if msg_type == "replay_stop":
                await replay_service.stop_replay(websocket)
                continue

            # ── 模式切换 ──────────────────────────────────
            if msg_type == "mode_switch":
                target = msg.get("target")
                if target in ("MANUAL", "SEMI_AUTO", "AUTO"):
                    await mqtt_bridge.publish_mode_cmd(target)
                continue

            # ── 控制指令 ──────────────────────────────────
            if msg_type == "control":
                action = msg.get("action")
                await mux_commander.submit(source="WEB", action=action)
                continue

    except WebSocketDisconnect:
        # 客户端断开时，若正在回放则取消任务
        if replay_service.is_replaying(websocket):
            await replay_service.stop_replay(websocket)
        manager.disconnect(websocket)
    except Exception:
        logger.exception("WebSocket 异常")
        if replay_service.is_replaying(websocket):
            await replay_service.stop_replay(websocket)
        manager.disconnect(websocket)


app.mount("/", StaticFiles(directory="../frontend", html=True), name="static")
