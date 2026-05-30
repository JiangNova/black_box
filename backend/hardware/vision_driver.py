"""
OAK-D 视觉驱动脚手架 · 目标检测数据发布器
════════════════════════════════════════════
独立进程：通过 MQTT 向 vehicle/telemetry 频道发布视觉目标检测结果。
发布频率：5 Hz

启动方式:
    python -m hardware.vision_driver

依赖:
    aiomqtt (已在 requirements.txt 中)

TODO: 暑假接入真实 OAK-D / DepthAI SDK 时，在此处替换为真实的读取逻辑。
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
PUBLISH_HZ = 5
PUBLISH_INTERVAL = 1.0 / PUBLISH_HZ
RECONNECT_DELAY = 3.0

logger = logging.getLogger("hardware.vision_driver")


# ══════════════════════════════════════════════════════
# TODO: 暑假接入真实 OAK-D / DepthAI SDK 时，替换以下函数
# ══════════════════════════════════════════════════════

def read_vision_detection() -> tuple[bool, str, float]:
    """
    从 OAK-D 相机读取一帧目标检测结果。

    TODO: 替换为真实 SDK 调用，例如:
        import depthai as dai
        detections = nn_node.get_output()
        if detections:
            label = detections[0].label
            distance = stereo_node.get_depth(detections[0])
            return True, label, distance
        return False, "", 0.0

    当前返回空检测 —— 无相机数据时不影响系统运行。
    """
    has_obstacle: bool = False
    obstacle_label: str = ""
    distance_m: float = 0.0
    return has_obstacle, obstacle_label, distance_m


# ══════════════════════════════════════════════════════


def build_payload(
    has_obstacle: bool, obstacle_label: str, distance_m: float
) -> dict:
    """
    构造符合 TelemetryFrame 规范的三通道 JSON 字典。

    视觉驱动仅负责 OAK-D 视觉通道，底盘与激光雷达通道以默认值填充，
    由 STM32 串口桥和 LiDAR 驱动独立注入各自的数据。
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
            "front_m": 0.0,
            "lidar_360": [],
        },
        "vision": {
            "has_obstacle": has_obstacle,
            "obstacle_label": obstacle_label,
            "distance_m": distance_m,
        },
        "timestamp": time.time_ns() // 1_000,
    }


async def main() -> None:
    """视觉驱动主循环：连接 MQTT → 定时发布 → 断线重连。"""
    logger.info(
        "Vision driver starting — %d Hz, broker=%s:%d",
        PUBLISH_HZ, BROKER_HOST, BROKER_PORT,
    )

    while True:
        try:
            async with Client(BROKER_HOST, BROKER_PORT) as client:
                logger.info("Vision driver connected to MQTT broker")

                while True:
                    # TODO: 暑假接入真实 OAK-D SDK 时，替换为真实的读取逻辑
                    has_obstacle, obstacle_label, distance_m = read_vision_detection()

                    payload = build_payload(has_obstacle, obstacle_label, distance_m)
                    await client.publish(
                        TOPIC_TELEMETRY, json.dumps(payload), qos=0
                    )

                    await asyncio.sleep(PUBLISH_INTERVAL)

        except MqttError as exc:
            logger.error("MQTT error: %s — reconnecting in %.0fs...", exc, RECONNECT_DELAY)
            await asyncio.sleep(RECONNECT_DELAY)
        except asyncio.CancelledError:
            logger.info("Vision driver shutting down")
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
        logger.info("Vision driver stopped by user")
