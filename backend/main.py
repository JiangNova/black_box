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
    "timestamp_us",
    "run_mode",
    "chassis_speed_mps",
    "chassis_steer_angle_deg",
    "imu_yaw",
    "imu_pitch",
    "imu_roll",
    "imu_gyro_z_rads",
    "lidar_front_m",
    "lidar_left_m",
    "lidar_right_m",
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

            if msg.get("type") == "mode_switch":
                target = msg.get("target")
                if target in ("MANUAL", "SEMI_AUTO", "AUTO"):
                    await mqtt_bridge.publish_mode_cmd(target)
                continue

            if msg.get("type") != "control":
                continue

            action = msg.get("action")
            await mux_commander.submit(source="WEB", action=action)
    except WebSocketDisconnect:
        manager.disconnect(websocket)


app.mount("/", StaticFiles(directory="../frontend", html=True), name="static")
