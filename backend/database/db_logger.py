import aiosqlite
import csv
import json
import os
from datetime import datetime

from database.models import TelemetryData

DB_DIR = "data"
DB_PATH = os.path.join(DB_DIR, "black_box.db")

TELEMETRY_DDL = """
    CREATE TABLE IF NOT EXISTS telemetry (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL,
        timestamp_us INTEGER NOT NULL,
        run_mode TEXT NOT NULL,
        chassis_speed_mps REAL,
        chassis_steer_angle_deg REAL,
        imu_yaw REAL,
        imu_pitch REAL,
        imu_roll REAL,
        imu_gyro_z_rads REAL,
        lidar_front_m REAL,
        lidar_left_m REAL,
        lidar_right_m REAL,
        lidar_360_json TEXT,
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
                if "timestamp_us" not in columns:
                    # 旧版表结构完全不同 → 整体重建
                    await db.execute("ALTER TABLE telemetry RENAME TO telemetry_legacy")
                    await db.execute(TELEMETRY_DDL)
                elif "lidar_360_json" not in columns:
                    # 新版表缺少 lidar_360_json 列 → 增量迁移
                    await db.execute(
                        "ALTER TABLE telemetry ADD COLUMN lidar_360_json TEXT"
                    )
                    print("📦 数据库迁移: 已添加 lidar_360_json 列")
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

    async def insert_telemetry(self, data: TelemetryData | dict):
        """MQTT 遥测落盘入口：接受嵌套模型或字典，展平写入 SQLite。"""
        if self.current_run_id is None:
            return

        if isinstance(data, dict):
            telemetry = TelemetryData.model_validate(data)
        else:
            telemetry = data

        # lidar_360 序列化为 JSON 字符串存入单列
        lidar_360 = getattr(telemetry.perception, "lidar_360", None)
        if lidar_360 is not None and isinstance(lidar_360, list):
            lidar_360_json = json.dumps(lidar_360, separators=(",", ":"))
        else:
            lidar_360_json = json.dumps([])

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """
                INSERT INTO telemetry (
                    run_id,
                    timestamp_us,
                    run_mode,
                    chassis_speed_mps,
                    chassis_steer_angle_deg,
                    imu_yaw,
                    imu_pitch,
                    imu_roll,
                    imu_gyro_z_rads,
                    lidar_front_m,
                    lidar_left_m,
                    lidar_right_m,
                    lidar_360_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.current_run_id,
                    telemetry.timestamp_us,
                    telemetry.run_mode,
                    telemetry.chassis.speed_mps,
                    telemetry.chassis.steer_angle_deg,
                    telemetry.imu.yaw,
                    telemetry.imu.pitch,
                    telemetry.imu.roll,
                    telemetry.imu.gyro_z_rads,
                    telemetry.perception.lidar_zones_m.front,
                    telemetry.perception.lidar_zones_m.left,
                    telemetry.perception.lidar_zones_m.right,
                    lidar_360_json,
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
                    timestamp_us,
                    run_mode,
                    chassis_speed_mps,
                    chassis_steer_angle_deg,
                    imu_yaw,
                    imu_pitch,
                    imu_roll,
                    imu_gyro_z_rads,
                    lidar_front_m,
                    lidar_left_m,
                    lidar_right_m,
                    lidar_360_json
                FROM telemetry
                WHERE run_id = ?
                ORDER BY timestamp_us ASC
                """,
                (run_id,),
            ) as cursor:
                async for row in cursor:
                    yield row

    def _row_to_telemetry_frame(self, row) -> dict:
        """
        将扁平 SQLite 行转换为与 MQTT 实时帧完全一致的嵌套结构，
        供回放推流使用，前端无需区分实时/回放数据格式。
        """
        lidar_360_raw = row["lidar_360_json"] if "lidar_360_json" in row.keys() else None
        try:
            lidar_360 = json.loads(lidar_360_raw) if lidar_360_raw else []
        except (json.JSONDecodeError, TypeError):
            lidar_360 = []

        return {
            "timestamp_us": row["timestamp_us"],
            "run_mode": row["run_mode"],
            "aeb_active": False,
            "chassis": {
                "speed_mps": row["chassis_speed_mps"],
                "steer_angle_deg": row["chassis_steer_angle_deg"],
            },
            "imu": {
                "yaw": row["imu_yaw"],
                "pitch": row["imu_pitch"],
                "roll": row["imu_roll"],
                "gyro_z_rads": row["imu_gyro_z_rads"],
            },
            "perception": {
                "lidar_zones_m": {
                    "front": row["lidar_front_m"],
                    "left": row["lidar_left_m"],
                    "right": row["lidar_right_m"],
                },
                "lidar_360": lidar_360,
            },
        }

    async def get_replay_frames(self, run_id: int):
        """
        异步生成器：逐帧 yield 嵌套格式的遥测帧，用于 WebSocket 回放推流。
        每帧格式与 MQTT 实时 broadcast 完全一致。
        """
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT
                    timestamp_us,
                    run_mode,
                    chassis_speed_mps,
                    chassis_steer_angle_deg,
                    imu_yaw,
                    imu_pitch,
                    imu_roll,
                    imu_gyro_z_rads,
                    lidar_front_m,
                    lidar_left_m,
                    lidar_right_m,
                    lidar_360_json
                FROM telemetry
                WHERE run_id = ?
                ORDER BY timestamp_us ASC
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
                        ORDER BY t.timestamp_us ASC
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
                    timestamp_us,
                    run_mode,
                    chassis_speed_mps,
                    chassis_steer_angle_deg,
                    imu_yaw,
                    imu_pitch,
                    imu_roll,
                    imu_gyro_z_rads,
                    lidar_front_m,
                    lidar_left_m,
                    lidar_right_m,
                    lidar_360_json
                FROM telemetry
                WHERE run_id = ?
                ORDER BY timestamp_us ASC
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
            "imu_yaw": [],
            "imu_pitch": [],
            "imu_roll": [],
            "gyro_z_rads": [],
            "lidar_front_m": [],
            "lidar_left_m": [],
            "lidar_right_m": [],
            "lidar_360": [],
        }

        for row in rows:
            columnar["timestamps"].append(row["timestamp_us"])
            columnar["run_modes"].append(row["run_mode"])
            columnar["speed_mps"].append(row["chassis_speed_mps"])
            columnar["steer_angle_deg"].append(row["chassis_steer_angle_deg"])
            columnar["imu_yaw"].append(row["imu_yaw"])
            columnar["imu_pitch"].append(row["imu_pitch"])
            columnar["imu_roll"].append(row["imu_roll"])
            columnar["gyro_z_rads"].append(row["imu_gyro_z_rads"])
            columnar["lidar_front_m"].append(row["lidar_front_m"])
            columnar["lidar_left_m"].append(row["lidar_left_m"])
            columnar["lidar_right_m"].append(row["lidar_right_m"])
            # 反序列化 lidar_360 点云
            lidar_raw = row["lidar_360_json"]
            try:
                columnar["lidar_360"].append(json.loads(lidar_raw) if lidar_raw else [])
            except (json.JSONDecodeError, TypeError):
                columnar["lidar_360"].append([])

        return columnar


db_logger = BlackBoxLogger()
