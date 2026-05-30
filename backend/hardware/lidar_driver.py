"""
LiDAR 驱动脚手架 · 激光雷达数据发布器
════════════════════════════════════════
独立进程：通过 MQTT 向 vehicle/telemetry 频道发布 LiDAR 点云数据。
发布频率：10 Hz

启动方式:
    python -m hardware.lidar_driver

依赖:
    aiomqtt (已在 requirements.txt 中)

TODO: 暑假接入真实 LiDAR SDK 时，在此处替换为真实的读取逻辑。
"""

import asyncio
import json
import logging
import os
import time

from aiomqtt import Client, MqttError

# ── 配置 ────────────────────────────────────────────
BROKER_HOST = os.getenv("MQTT_HOST", "127.0.0.1")
BROKER_PORT = int(os.getenv("MQTT_PORT", "1883"))
TOPIC_TELEMETRY = "vehicle/telemetry"
PUBLISH_HZ = 10
PUBLISH_INTERVAL = 1.0 / PUBLISH_HZ
RECONNECT_DELAY = 3.0

logger = logging.getLogger("hardware.lidar_driver")


# ══════════════════════════════════════════════════════
# TODO: 暑假接入真实 LiDAR SDK 时，替换以下函数
# ══════════════════════════════════════════════════════

def read_lidar_scan() -> list[float]:
    """
    读取一帧 360° 激光雷达扫描数据。

    TODO: 替换为真实 SDK 调用，例如:
        import rplidar
        scan = lidar.iter_measures()
        return [p.distance for p in scan]

    当前返回空列表 —— 无雷达数据时不影响系统运行。
    """
    return []


def read_front_distance() -> float:
    """
    读取前向避障距离 (米)。

    TODO: 替换为真实 SDK 调用，例如:
        import vl53l1x
        return tof_sensor.read_distance() / 1000.0

    当前返回 0.0 —— 表示无前向障碍物数据。
    """
    return 0.0


# ══════════════════════════════════════════════════════


def build_payload(lidar_360: list[float], front_m: float) -> dict:
    """
    构造符合 TelemetryFrame 规范的三通道 JSON 字典。

    LiDAR 驱动仅负责激光雷达通道，底盘与视觉通道以默认值填充，
    由 STM32 串口桥和视觉驱动独立注入各自的数据。
    """
    return {
        "chassis": {
            "speed_mps": 0.0,
            "gyro_z_rads": 0.0,
            "yaw": 0.0,
            "pitch": 0.0,
            "roll": 0.0,
            "run_mode": "MANUAL",
            "aeb_active": False,
            "steer_angle_deg": 0.0,
        },
        "lidar": {
            "front_m": front_m,
            "lidar_360": lidar_360,
        },
        "vision": {
            "has_obstacle": False,
            "obstacle_label": "",
            "distance_m": 0.0,
        },
        "timestamp": time.time_ns() // 1_000,
    }


async def main() -> None:
    """LiDAR 驱动主循环：连接 MQTT → 定时发布 → 断线重连。"""
    logger.info(
        "LiDAR driver starting — %d Hz, broker=%s:%d",
        PUBLISH_HZ, BROKER_HOST, BROKER_PORT,
    )

    while True:
        try:
            async with Client(BROKER_HOST, BROKER_PORT) as client:
                logger.info("LiDAR driver connected to MQTT broker")

                while True:
                    # TODO: 暑假接入真实 LiDAR SDK 时，替换为真实的读取逻辑
                    lidar_360 = read_lidar_scan()
                    front_m = read_front_distance()

                    payload = build_payload(lidar_360, front_m)
                    await client.publish(
                        TOPIC_TELEMETRY, json.dumps(payload), qos=0
                    )

                    await asyncio.sleep(PUBLISH_INTERVAL)

        except MqttError as exc:
            logger.error("MQTT error: %s — reconnecting in %.0fs...", exc, RECONNECT_DELAY)
            await asyncio.sleep(RECONNECT_DELAY)
        except asyncio.CancelledError:
            logger.info("LiDAR driver shutting down")
            break
        except Exception:
            logger.exception("Unexpected error — reconnecting in %.0fs...", RECONNECT_DELAY)
            await asyncio.sleep(RECONNECT_DELAY)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("LiDAR driver stopped by user")
