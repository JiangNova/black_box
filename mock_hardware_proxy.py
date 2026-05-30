"""
mock_hardware_proxy.py
──────────────────────
有状态物理模拟器：20Hz 上行遥测 + 异步下行控制监听 + AEB 脊髓反射层。
使用 asyncio.Event 作为急停全局状态，下行 set() 后上行下一帧立即归零。

AEB 触发条件：处于 SEMI_AUTO 或 AUTO 模式，且前向雷达 ≤ 0.20m。
AEB 解除条件：切回 MANUAL（人类最高接管权）或前向雷达恢复至 > 0.50m。

v2.0 · Tri-Channel 三通道嵌套格式：
  { chassis: {...}, lidar: {...}, vision: {...}, timestamp: ... }

用法:
    python mock_hardware_proxy.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import sys
import time

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from aiomqtt import Client

BROKER = "127.0.0.1"
PORT = 1883
TOPIC_TELEMETRY = "vehicle/telemetry"
TOPIC_CONTROL = "vehicle/control_cmd"
TOPIC_MODE_CMD = "vehicle/mode_cmd"
PUBLISH_HZ = 20
INTERVAL_S = 1.0 / PUBLISH_HZ

AEB_TRIGGER_M = 0.20
AEB_RELEASE_M = 0.50

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("mock_hardware_proxy")

# 默认未 set → 正常运行；set 后 → 急停锁死
estop_event = asyncio.Event()

# 运行模式状态（由下行 mode_switch 指令更新）
current_mode = "MANUAL"

# AEB 锁死状态（脊髓反射层）
aeb_active = False

# ── Vision 假数据状态机 ──────────────────────────────────
_vision_label_pool = ["", "box", "person", "cone", "barrier"]
_vision_timer = 0.0


def _generate_vision(t: float, front_m: float) -> dict:
    """模拟 OAK-D 视觉识别：每隔约 3 秒随机出现目标，持续约 2 秒。"""
    global _vision_timer

    period = 3.0 + 0.5 * math.sin(t * 0.3)
    cycle_pos = (t % period) / period

    if cycle_pos < 0.6:
        # 空闲期：无目标
        return {
            "has_obstacle": False,
            "obstacle_label": "",
            "distance_m": 0.0,
        }

    # 激活期：随机选一个标签，距离与雷达前向测距联动（加少许噪声）
    label = _vision_label_pool[random.randint(1, len(_vision_label_pool) - 1)]
    dist = max(0.2, front_m + random.uniform(-0.3, 0.3))
    return {
        "has_obstacle": True,
        "obstacle_label": label,
        "distance_m": round(dist, 2),
    }


def build_telemetry_frame(t0: float) -> dict:
    """
    简易状态机物理引擎：急停 / AEB 锁死时运动学量强制归零。

    Returns
    -------
    dict  三通道嵌套遥测帧 (TelemetryFrame 格式)
    """
    global aeb_active

    t = time.monotonic() - t0

    if estop_event.is_set():
        speed_mps = 0.0
        gyro_z_rads = 0.0
        front_m = 5.0
        run_mode = "ESTOP"
        aeb_active = False
    else:
        # ── 原始物理计算 ──────────────────────────────────
        raw_speed = 1.25 + 0.75 * math.sin(t * 0.8)
        gyro_z_rads = 0.15 * math.sin(t * 1.5)

        if current_mode == "MANUAL":
            front_m = max(0.35, 4.2 - raw_speed * 1.4 + 0.25 * math.sin(t * 1.1))
        else:
            # SEMI_AUTO / AUTO：允许障碍物逼近至危险距离，触发 AEB
            front_m = max(0.03, 2.5 - raw_speed * 1.8 + 0.25 * math.sin(t * 1.1))

        # ── AEB 脊髓反射判决（带迟滞）────────────────────
        if current_mode in ("SEMI_AUTO", "AUTO") and front_m <= AEB_TRIGGER_M:
            if not aeb_active:
                logger.critical(
                    "\033[91m🚨 AEB 介入！前向障碍物 %.2f m ≤ %.2f m，"
                    "底层强制截断动力！\033[0m",
                    front_m,
                    AEB_TRIGGER_M,
                )
            aeb_active = True
        elif current_mode == "MANUAL" or front_m > AEB_RELEASE_M:
            if aeb_active:
                logger.info(
                    "\033[92m✅ AEB 解除：mode=%s front=%.2f m\033[0m",
                    current_mode,
                    front_m,
                )
            aeb_active = False

        # ── AEB 力学校正 ──────────────────────────────────
        if aeb_active:
            speed_mps = 0.0
        else:
            speed_mps = raw_speed

        run_mode = current_mode

    # ── 360° 激光雷达极坐标点云 (BEV) ─────────────────
    # 关键：以帧计数器 t 为相位驱动源，配合 sin 波动 + 高斯噪声，
    # 确保每一帧的 360 个散点都有微观位移，前端 ECharts 才能高频重绘。
    lidar_360 = []
    for i in range(360):
        shape_wave = 0.5 * math.sin(t * 2.0 + i / 20.0)
        noise = random.uniform(-0.1, 0.1)
        radius = max(0.15, round(2.0 + shape_wave + noise, 2))
        lidar_360.append([i, radius])

    # ── OAK-D 视觉假数据 ──────────────────────────────
    vision = _generate_vision(t, front_m)

    # ── 三通道嵌套组装 ─────────────────────────────────
    return {
        "chassis": {
            "speed_mps": round(speed_mps, 3),
            "gyro_z_rads": round(gyro_z_rads, 4),
            "yaw": 0.0,
            "pitch": 0.0,
            "roll": 0.0,
            "run_mode": run_mode,
            "aeb_active": aeb_active,
            "steer_angle_deg": 0.0,
        },
        "lidar": {
            "front_m": round(front_m, 2),
            "lidar_360": lidar_360,
        },
        "vision": vision,
        "timestamp": time.time_ns() // 1_000,
    }


async def upstream_worker(client: Client) -> None:
    """20Hz 遥测上行；每帧发送前读取 estop_event 与 AEB 最新状态。"""
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
        lidar = frame["lidar"]
        vision = frame["vision"]
        tags = []
        if estop_event.is_set():
            tags.append("ESTOP")
        if chassis["aeb_active"]:
            tags.append("AEB")
        tag_str = f" [{'|'.join(tags)}]" if tags else ""
        vision_str = ""
        if vision["has_obstacle"]:
            vision_str = (
                f" | vision: {vision['obstacle_label']} @ {vision['distance_m']}m"
            )
        logger.info(
            "TX #%d%s | mode=%s speed=%.3f m/s | gyro_z=%.4f rad/s | "
            "lidar front=%.2f m%s",
            frame_count,
            tag_str,
            chassis["run_mode"],
            chassis["speed_mps"],
            chassis["gyro_z_rads"],
            lidar["front_m"],
            vision_str,
        )

        next_tick += INTERVAL_S
        sleep_s = next_tick - time.monotonic()
        if sleep_s > 0:
            await asyncio.sleep(sleep_s)
        else:
            next_tick = time.monotonic()


async def downstream_worker(client: Client) -> None:
    """订阅下行控制主题与模式切换主题；维护 current_mode 与 AEB 状态。"""
    global current_mode, aeb_active

    await client.subscribe(TOPIC_CONTROL)
    await client.subscribe(TOPIC_MODE_CMD)
    logger.info(
        "Downstream started: subscribed to %s + %s",
        TOPIC_CONTROL,
        TOPIC_MODE_CMD,
    )

    async for message in client.messages:
        topic = str(message.topic)

        try:
            cmd = json.loads(message.payload.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("Invalid payload on %s: %r", topic, message.payload)
            continue

        # ── 模式切换指令 ──────────────────────────────────
        if topic == TOPIC_MODE_CMD or cmd.get("type") == "mode_switch":
            target = cmd.get("target", "")
            if target in ("MANUAL", "SEMI_AUTO", "AUTO"):
                if target != current_mode:
                    logger.info(
                        "模式切换: %s → %s", current_mode, target
                    )
                    current_mode = target
                # 人类接管：切回 MANUAL 立即解除 AEB
                if target == "MANUAL" and aeb_active:
                    aeb_active = False
                    logger.info(
                        "\033[92m🛡️  人类接管 → AEB 强制解除\033[0m"
                    )
            continue

        if topic != TOPIC_CONTROL:
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
            elif aeb_active:
                logger.warning(
                    "移动指令 %s 被 AEB 拦截：脊髓反射锁死中", action
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
