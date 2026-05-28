"""
mock_hardware_proxy.py
──────────────────────
有状态物理模拟器：20Hz 上行遥测 + 异步下行控制监听。
使用 asyncio.Event 作为急停全局状态，下行 set() 后上行下一帧立即归零。

用法:
    python mock_hardware_proxy.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import sys
import time

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from aiomqtt import Client

BROKER = "127.0.0.1"
PORT = 1883
TOPIC_TELEMETRY = "vehicle/telemetry"
TOPIC_CONTROL = "vehicle/control_cmd"
PUBLISH_HZ = 20
INTERVAL_S = 1.0 / PUBLISH_HZ

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("mock_hardware_proxy")

# 默认未 set → 正常运行；set 后 → 急停锁死
estop_event = asyncio.Event()


def build_telemetry_frame(t0: float) -> dict:
    """简易状态机物理引擎：急停时运动学量强制归零，无残留。"""
    t = time.monotonic() - t0

    if estop_event.is_set():
        speed_mps = 0.0
        gyro_z_rads = 0.0
        front_m = 5.0
        left_m = 5.0
        right_m = 5.0
        run_mode = "ESTOP"
    else:
        speed_mps = 1.25 + 0.75 * math.sin(t * 0.8)
        gyro_z_rads = 0.15 * math.sin(t * 1.5)
        front_m = max(0.35, 4.2 - speed_mps * 1.4 + 0.25 * math.sin(t * 1.1))
        left_m = 1.8 + 0.6 * math.cos(t * 0.7)
        right_m = 1.8 + 0.6 * math.sin(t * 0.9)
        run_mode = "MANUAL"

    return {
        "timestamp_us": time.time_ns() // 1_000,
        "run_mode": run_mode,
        "chassis": {
            "speed_mps": round(speed_mps, 3),
            "steer_angle_deg": 0.0,
        },
        "imu": {
            "yaw": 0.0,
            "pitch": 0.0,
            "roll": 0.0,
            "gyro_z_rads": round(gyro_z_rads, 4),
        },
        "perception": {
            "lidar_zones_m": {
                "front": round(front_m, 2),
                "left": round(left_m, 2),
                "right": round(right_m, 2),
            }
        },
    }


async def upstream_worker(client: Client) -> None:
    """20Hz 遥测上行；每帧发送前读取 estop_event 最新状态。"""
    t0 = time.monotonic()
    frame_count = 0
    next_tick = time.monotonic()

    logger.info(
        "Upstream started: publishing to %s @ %d Hz",
        TOPIC_TELEMETRY,
        PUBLISH_HZ,
    )

    while True:
        frame = build_telemetry_frame(t0)
        payload = json.dumps(frame, separators=(",", ":"))
        await client.publish(TOPIC_TELEMETRY, payload)
        frame_count += 1

        chassis = frame["chassis"]
        imu = frame["imu"]
        lidar = frame["perception"]["lidar_zones_m"]
        estop_tag = " [ESTOP]" if estop_event.is_set() else ""
        logger.info(
            "TX #%d%s | speed=%.3f m/s | gyro_z=%.4f rad/s | "
            "lidar front/left/right=%.2f/%.2f/%.2f m",
            frame_count,
            estop_tag,
            chassis["speed_mps"],
            imu["gyro_z_rads"],
            lidar["front"],
            lidar["left"],
            lidar["right"],
        )

        next_tick += INTERVAL_S
        sleep_s = next_tick - time.monotonic()
        if sleep_s > 0:
            await asyncio.sleep(sleep_s)
        else:
            next_tick = time.monotonic()


async def downstream_worker(client: Client) -> None:
    """订阅下行控制主题；E_STOP 时 set() 全局急停事件。"""
    await client.subscribe(TOPIC_CONTROL)
    logger.info("Downstream started: subscribed to %s", TOPIC_CONTROL)

    async for message in client.messages:
        if str(message.topic) != TOPIC_CONTROL:
            continue

        try:
            cmd = json.loads(message.payload.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("Invalid control payload: %r", message.payload)
            continue

        action = cmd.get("action")

        if action == "E_STOP":
            estop_event.set()
            logger.critical(
                "\033[91m🚨 收到最高指令：底盘急停抱死！"
                " [estop_event.set()]\033[0m"
            )
            continue

        if action in ("FORWARD", "BACKWARD", "LEFT", "RIGHT"):
            if estop_event.is_set():
                logger.warning(
                    "移动指令 %s 被忽略：底盘处于急停锁死状态", action
                )
            else:
                logger.info("收到移动指令: action=%s", action)
            continue

        logger.info("收到未知控制指令: %s", cmd)


async def run_proxy() -> None:
    async with Client(BROKER, PORT) as client:
        logger.info("Connected to MQTT broker %s:%d", BROKER, PORT)
        await asyncio.gather(
            upstream_worker(client),
            downstream_worker(client),
        )


async def main() -> None:
    try:
        await run_proxy()
    except KeyboardInterrupt:
        logger.info("Mock hardware proxy stopped by user")
    except Exception:
        logger.exception("Mock hardware proxy crashed")
        raise


if __name__ == "__main__":
    asyncio.run(main())
