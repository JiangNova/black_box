import aiosqlite
import csv
import json
import os
from datetime import datetime

from database.models import TelemetryFrame

DB_DIR = "data"
DB_PATH = os.path.join(DB_DIR, "black_box.db")

TELEMETRY_DDL = """
    CREATE TABLE IF NOT EXISTS telemetry (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL,
        timestamp INTEGER NOT NULL,
        run_mode TEXT NOT NULL,
        chassis_speed_mps REAL,
        chassis_steer_angle_deg REAL,
        chassis_gyro_z_rads REAL,
        chassis_yaw REAL,
        chassis_pitch REAL,
        chassis_roll REAL,
        chassis_aeb_active INTEGER,
        lidar_front_m REAL,
        lidar_360_json TEXT,
        vision_has_obstacle INTEGER,
        vision_obstacle_label TEXT,
        vision_distance_m REAL,
        receive_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(run_id) REFERENCES runs(id)
    )
"""


class BlackBoxLogger:
    def __init__(self):
        self.current_run_id = None
        os.makedirs(DB_DIR, exist_ok=True)

    async def init_db(self):
        """建库建表；若检测到旧版 telemetry 表结构则自动迁移。"""
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    start_time TEXT NOT NULL,
                    note TEXT
                )
            """)

            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='telemetry'"
            )
            exists = await cursor.fetchone()

            if exists:
                info = await db.execute("PRAGMA table_info(telemetry)")
                columns = {row[1] for row in await info.fetchall()}
                # 检测旧版表结构（以旧字段 timestamp_us 为特征）
                if "timestamp_us" in columns or "timestamp" not in columns:
                    await db.execute("ALTER TABLE telemetry RENAME TO telemetry_legacy")
                    await db.execute(TELEMETRY_DDL)
                elif "lidar_360_json" not in columns:
                    # 新版表缺少 lidar_360_json 列 → 增量迁移
                    await db.execute(
                        "ALTER TABLE telemetry ADD COLUMN lidar_360_json TEXT"
                    )
                    print("📦 数据库迁移: 已添加 lidar_360_json 列")
                if "vision_has_obstacle" not in columns:
                    # 新版表缺少 vision 列 → 增量迁移
                    await db.execute(
                        "ALTER TABLE telemetry ADD COLUMN vision_has_obstacle INTEGER DEFAULT 0"
                    )
                    await db.execute(
                        "ALTER TABLE telemetry ADD COLUMN vision_obstacle_label TEXT DEFAULT ''"
                    )
                    await db.execute(
                        "ALTER TABLE telemetry ADD COLUMN vision_distance_m REAL DEFAULT 0.0"
                    )
                    print("📦 数据库迁移: 已添加 vision 三列")
            else:
                await db.execute(TELEMETRY_DDL)

            await db.commit()
        print("📦 数据库初始化完成: data/black_box.db")

    async def start_new_run(self, note="Standard Test"):
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "INSERT INTO runs (start_time, note) VALUES (?, ?)",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), note),
            )
            await db.commit()
            self.current_run_id = cursor.lastrowid
        print(f"🚀 开始新批次记录，Run ID: {self.current_run_id}")
        return self.current_run_id

    async def log_data(self, data_dict: dict):
        await self.insert_telemetry(data_dict)

    async def insert_telemetry(self, data: TelemetryFrame | dict):
        """MQTT 遥测落盘入口：接受三通道嵌套模型或字典，展平写入 SQLite。"""
        if self.current_run_id is None:
            return

        if isinstance(data, dict):
            telemetry = TelemetryFrame.model_validate(data)
        else:
            telemetry = data

        # lidar_360 序列化为 JSON 字符串存入单列（供回放使用）
        lidar_360 = telemetry.lidar.lidar_360
        if lidar_360 is not None and isinstance(lidar_360, list):
            lidar_360_json = json.dumps(lidar_360, separators=(",", ":"))
        else:
            lidar_360_json = json.dumps([])

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """
                INSERT INTO telemetry (
                    run_id,
                    timestamp,
                    run_mode,
                    chassis_speed_mps,
                    chassis_steer_angle_deg,
                    chassis_gyro_z_rads,
                    chassis_yaw,
                    chassis_pitch,
                    chassis_roll,
                    chassis_aeb_active,
                    lidar_front_m,
                    lidar_360_json,
                    vision_has_obstacle,
                    vision_obstacle_label,
                    vision_distance_m
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.current_run_id,
                    telemetry.timestamp,
                    telemetry.chassis.run_mode,
                    telemetry.chassis.speed_mps,
                    telemetry.chassis.steer_angle_deg,
                    telemetry.chassis.gyro_z_rads,
                    telemetry.chassis.yaw,
                    telemetry.chassis.pitch,
                    telemetry.chassis.roll,
                    int(telemetry.chassis.aeb_active),
                    telemetry.lidar.front_m,
                    lidar_360_json,
                    int(telemetry.vision.has_obstacle),
                    telemetry.vision.obstacle_label,
                    telemetry.vision.distance_m,
                ),
            )
            await db.commit()

    async def export_to_csv(self, run_id: int, filename: str):
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM telemetry WHERE run_id = ?", (run_id,)
            ) as cursor:
                rows = await cursor.fetchall()

                if rows:
                    with open(filename, "w", newline="", encoding="utf-8") as f:
                        writer = csv.writer(f)
                        writer.writerow(rows[0].keys())
                        for row in rows:
                            writer.writerow(row)
                    print(f"✅ 成功导出 {len(rows)} 条数据到 {filename}")

    async def export_telemetry_rows(self, run_id: int):
        """
        流式异步生成器：逐行 yield 遥测数据，供 StreamingResponse 使用。
        不会一次性将所有行加载到内存，适合数万条高频数据场景。
        """
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT
                    timestamp,
                    run_mode,
                    chassis_speed_mps,
                    chassis_steer_angle_deg,
                    chassis_gyro_z_rads,
                    chassis_yaw,
                    chassis_pitch,
                    chassis_roll,
                    chassis_aeb_active,
                    lidar_front_m,
                    vision_has_obstacle,
                    vision_obstacle_label,
                    vision_distance_m
                FROM telemetry
                WHERE run_id = ?
                ORDER BY timestamp ASC
                """,
                (run_id,),
            ) as cursor:
                async for row in cursor:
                    yield row

    def _row_to_telemetry_frame(self, row) -> dict:
        """
        将扁平 SQLite 行转换为与 MQTT 实时帧完全一致的三通道嵌套结构，
        供回放推流使用，前端无需区分实时/回放数据格式。
        """
        lidar_360_raw = row["lidar_360_json"] if "lidar_360_json" in row.keys() else None
        try:
            lidar_360 = json.loads(lidar_360_raw) if lidar_360_raw else []
        except (json.JSONDecodeError, TypeError):
            lidar_360 = []

        vision_has = row["vision_has_obstacle"] if "vision_has_obstacle" in row.keys() else 0
        vision_label = row["vision_obstacle_label"] if "vision_obstacle_label" in row.keys() else ""
        vision_dist = row["vision_distance_m"] if "vision_distance_m" in row.keys() else 0.0

        return {
            "chassis": {
                "speed_mps": row["chassis_speed_mps"],
                "steer_angle_deg": row["chassis_steer_angle_deg"],
                "gyro_z_rads": row["chassis_gyro_z_rads"],
                "yaw": row["chassis_yaw"],
                "pitch": row["chassis_pitch"],
                "roll": row["chassis_roll"],
                "run_mode": row["run_mode"],
                "aeb_active": bool(row["chassis_aeb_active"]),
            },
            "lidar": {
                "front_m": row["lidar_front_m"],
                "lidar_360": lidar_360,
            },
            "vision": {
                "has_obstacle": bool(vision_has),
                "obstacle_label": vision_label,
                "distance_m": vision_dist,
            },
            "timestamp": row["timestamp"],
        }

    async def get_replay_frames(self, run_id: int):
        """
        异步生成器：逐帧 yield 三通道嵌套格式的遥测帧，用于 WebSocket 回放推流。
        每帧格式与 MQTT 实时 broadcast 完全一致。
        """
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT
                    timestamp,
                    run_mode,
                    chassis_speed_mps,
                    chassis_steer_angle_deg,
                    chassis_gyro_z_rads,
                    chassis_yaw,
                    chassis_pitch,
                    chassis_roll,
                    chassis_aeb_active,
                    lidar_front_m,
                    lidar_360_json,
                    vision_has_obstacle,
                    vision_obstacle_label,
                    vision_distance_m
                FROM telemetry
                WHERE run_id = ?
                ORDER BY timestamp ASC
                """,
                (run_id,),
            ) as cursor:
                async for row in cursor:
                    yield self._row_to_telemetry_frame(row)

    async def get_all_runs(self) -> list[dict]:
        """查询所有实验批次，按 run_id 倒序（最新在前）。"""
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT
                    r.id AS run_id,
                    r.start_time,
                    r.note,
                    (
                        SELECT t.run_mode
                        FROM telemetry t
                        WHERE t.run_id = r.id
                        ORDER BY t.timestamp ASC
                        LIMIT 1
                    ) AS run_mode,
                    (
                        SELECT COUNT(*)
                        FROM telemetry t
                        WHERE t.run_id = r.id
                    ) AS sample_count
                FROM runs r
                ORDER BY r.id DESC
                """
            ) as cursor:
                rows = await cursor.fetchall()

        return [dict(row) for row in rows]

    async def run_exists(self, run_id: int) -> bool:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT 1 FROM runs WHERE id = ? LIMIT 1", (run_id,)
            )
            return await cursor.fetchone() is not None

    async def get_telemetry_by_run(self, run_id: int) -> dict | None:
        """
        查询指定批次的全部遥测，转换为列式 JSON 以压缩传输体积。
        run 不存在时返回 None。
        """
        if not await self.run_exists(run_id):
            return None

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT
                    timestamp,
                    run_mode,
                    chassis_speed_mps,
                    chassis_steer_angle_deg,
                    chassis_gyro_z_rads,
                    chassis_yaw,
                    chassis_pitch,
                    chassis_roll,
                    chassis_aeb_active,
                    lidar_front_m,
                    vision_has_obstacle,
                    vision_obstacle_label,
                    vision_distance_m
                FROM telemetry
                WHERE run_id = ?
                ORDER BY timestamp ASC
                """,
                (run_id,),
            ) as cursor:
                rows = await cursor.fetchall()

        columnar = {
            "run_id": run_id,
            "count": len(rows),
            "timestamps": [],
            "run_modes": [],
            "speed_mps": [],
            "steer_angle_deg": [],
            "gyro_z_rads": [],
            "imu_yaw": [],
            "imu_pitch": [],
            "imu_roll": [],
            "aeb_active": [],
            "lidar_front_m": [],
            "vision_has_obstacle": [],
            "vision_obstacle_label": [],
            "vision_distance_m": [],
        }

        for row in rows:
            columnar["timestamps"].append(row["timestamp"])
            columnar["run_modes"].append(row["run_mode"])
            columnar["speed_mps"].append(row["chassis_speed_mps"])
            columnar["steer_angle_deg"].append(row["chassis_steer_angle_deg"])
            columnar["gyro_z_rads"].append(row["chassis_gyro_z_rads"])
            columnar["imu_yaw"].append(row["chassis_yaw"])
            columnar["imu_pitch"].append(row["chassis_pitch"])
            columnar["imu_roll"].append(row["chassis_roll"])
            columnar["aeb_active"].append(bool(row["chassis_aeb_active"]))
            columnar["lidar_front_m"].append(row["lidar_front_m"])
            columnar["vision_has_obstacle"].append(bool(row["vision_has_obstacle"]))
            columnar["vision_obstacle_label"].append(row["vision_obstacle_label"])
            columnar["vision_distance_m"].append(row["vision_distance_m"])

        return columnar


db_logger = BlackBoxLogger()
