"""
STM32 串口解析器 · 工业级底盘遥测数据采集模块
═══════════════════════════════════════════════════
用于对接真实 STM32 底盘，替换 mock 数据源。
读取串口原始字节流 → 解析 STM32 二进制协议帧 → 组装 TelemetryFrame JSON → 发布到 MQTT

依赖安装:
  pip install pyserial
  (aiomqtt 已在 requirements.txt 中)

═══════════════════════════════════════════════════
通信协议 —— 大端字节序 (Big-Endian / Network Byte Order)

  Byte  |  Field              |  Type     |  说明
  ──────┼─────────────────────┼───────────┼─────────────────────
  [0]   |  header_1           |  uint8    |  帧头1 固定 0xAA
  [1]   |  header_2           |  uint8    |  帧头2 固定 0x55
  [2]   |  frame_type         |  uint8    |  数据类型 0x01=底盘遥测
  [3-6] |  speed_mps          |  float32  |  线速度 (m/s), 大端
  [7-10]|  gyro_z_rads        |  float32  |  Z轴角速度 (rad/s), 大端
  [11]  |  run_mode           |  uint8    |  0=MANUAL, 1=SEMI_AUTO, 2=FULL_AUTO
  [12]  |  reserved_1         |  uint8    |  预留
  [13]  |  reserved_2         |  uint8    |  预留
  [14]  |  reserved_3         |  uint8    |  预留
  [15]  |  checksum           |  uint8    |  前15字节累加和的低8位

  总帧长: 16 字节
  校验算法: checksum = (sum(bytes[0:15]) & 0xFF)
═══════════════════════════════════════════════════
"""

import asyncio
import json
import logging
import struct
import threading
import time
from typing import Callable, Optional

import serial
import serial.tools.list_ports

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════
# 协议常量
# ═══════════════════════════════════════════════════

FRAME_HEADER_1 = 0xAA
FRAME_HEADER_2 = 0x55
FRAME_TYPE_TELEMETRY = 0x01
FRAME_LENGTH = 16
CHECKSUM_BYTE_INDEX = 15

# 运行模式映射 (STM32 uint8 → TelemetryFrame 字符串)
_RUN_MODE_LUT = {
    0: "MANUAL",
    1: "SEMI_AUTO",
    2: "FULL_AUTO",
}


# ═══════════════════════════════════════════════════
# 帧解析状态机
# ═══════════════════════════════════════════════════

class _ParserState:
    """状态机内部状态枚举"""
    SEEK_HEADER_1 = 0  # 寻找第一个帧头 0xAA
    SEEK_HEADER_2 = 1  # 寻找第二个帧头 0x55
    READ_PAYLOAD = 2   # 读取剩余负载字节


class ParsedFrame:
    """一帧解析完成的 STM32 遥测数据"""

    __slots__ = ("speed_mps", "gyro_z_rads", "run_mode", "checksum", "raw")

    def __init__(
        self,
        speed_mps: float,
        gyro_z_rads: float,
        run_mode: int,
        checksum: int,
        raw: bytes,
    ):
        self.speed_mps = speed_mps
        self.gyro_z_rads = gyro_z_rads
        self.run_mode = run_mode
        self.checksum = checksum
        self.raw = raw

    def to_telemetry_dict(self) -> dict:
        """
        转换为 TelemetryFrame.model_validate() 兼容的字典。

        Lidar 和 Vision 通道置为默认空值 —— STM32 仅提供底盘遥测，
        激光雷达与视觉数据由各自的独立通道注入。
        """
        return {
            "chassis": {
                "speed_mps": round(self.speed_mps, 6),
                "gyro_z_rads": round(self.gyro_z_rads, 6),
                "yaw": 0.0,
                "pitch": 0.0,
                "roll": 0.0,
                "run_mode": _RUN_MODE_LUT.get(self.run_mode, "MANUAL"),
                "aeb_active": False,
                "steer_angle_deg": 0.0,
            },
            "lidar": {
                "front_m": 5.0,
                "lidar_360": [],
            },
            "vision": {
                "has_obstacle": False,
                "obstacle_label": "",
                "distance_m": 0.0,
            },
            "timestamp": time.time_ns() // 1_000,
        }


class STM32FrameParser:
    """
    逐字节状态机解析器。

    用法:
        parser = STM32FrameParser()
        for byte in serial_stream:
            frame = parser.feed(byte)
            if frame:
                process(frame)
    """

    def __init__(self):
        self._state = _ParserState.SEEK_HEADER_1
        self._buffer = bytearray()

    # --------------------------------------------------
    # 公开 API
    # --------------------------------------------------

    def reset(self) -> None:
        """重置状态机（串口重连后调用以避免半帧污染）"""
        self._state = _ParserState.SEEK_HEADER_1
        self._buffer.clear()

    def feed(self, byte: int) -> Optional[ParsedFrame]:
        """
        喂入一个字节，如果凑齐完整且校验通过的一帧则返回 ParsedFrame，
        否则返回 None。

        校验失败时自动丢弃该帧并在日志中记录。
        """
        state = self._state

        if state == _ParserState.SEEK_HEADER_1:
            return self._handle_seek_h1(byte)

        elif state == _ParserState.SEEK_HEADER_2:
            return self._handle_seek_h2(byte)

        elif state == _ParserState.READ_PAYLOAD:
            return self._handle_read_payload(byte)

        # 不应到达这里；防御性重置
        logger.error("Parser entered unknown state %s; resetting", state)
        self.reset()
        return None

    # --------------------------------------------------
    # 状态处理
    # --------------------------------------------------

    def _handle_seek_h1(self, byte: int) -> Optional[ParsedFrame]:
        if byte == FRAME_HEADER_1:
            self._buffer = bytearray([byte])
            self._state = _ParserState.SEEK_HEADER_2
        # 否则静默丢弃（非帧头字节）
        return None

    def _handle_seek_h2(self, byte: int) -> Optional[ParsedFrame]:
        if byte == FRAME_HEADER_2:
            self._buffer.append(byte)
            self._state = _ParserState.READ_PAYLOAD
            return None

        # 不是 0x55：回退到 seek_h1
        self._state = _ParserState.SEEK_HEADER_1
        self._buffer.clear()
        # 但当前字节自身可能是 0xAA，需要递归处理
        if byte == FRAME_HEADER_1:
            self._buffer = bytearray([byte])
            self._state = _ParserState.SEEK_HEADER_2
        return None

    def _handle_read_payload(self, byte: int) -> Optional[ParsedFrame]:
        self._buffer.append(byte)
        if len(self._buffer) < FRAME_LENGTH:
            return None

        # 帧已凑齐 → 解析
        frame = self._parse_full_frame(self._buffer)
        self.reset()
        return frame

    # --------------------------------------------------
    # 帧解码
    # --------------------------------------------------

    def _parse_full_frame(self, raw: bytearray) -> Optional[ParsedFrame]:
        """校验 + 解包完整 16 字节帧；失败返回 None"""

        # --- checksum 校验 ---
        calc_sum = sum(raw[:CHECKSUM_BYTE_INDEX]) & 0xFF
        frame_checksum = raw[CHECKSUM_BYTE_INDEX]
        if calc_sum != frame_checksum:
            logger.warning(
                "Checksum mismatch — calculated=0x%02X received=0x%02X | raw=%s",
                calc_sum,
                frame_checksum,
                raw.hex(" "),
            )
            return None

        # --- 数据类型白名单 ---
        frame_type = raw[2]
        if frame_type != FRAME_TYPE_TELEMETRY:
            logger.info(
                "Ignoring non-telemetry frame type 0x%02X (only 0x01 supported)",
                frame_type,
            )
            return None

        # --- 大端解包 float32 ---
        try:
            speed_mps = struct.unpack(">f", bytes(raw[3:7]))[0]
            gyro_z_rads = struct.unpack(">f", bytes(raw[7:11]))[0]
        except struct.error as exc:
            logger.error("struct.unpack failed on frame %s: %s", raw.hex(" "), exc)
            return None

        # --- NaN / Inf 过滤 ---
        for val, name in [(speed_mps, "speed_mps"), (gyro_z_rads, "gyro_z_rads")]:
            if not _is_finite(val):
                logger.warning(
                    "Non-finite %s=%.4f in frame, discarding", name, val
                )
                return None

        run_mode = raw[11]

        return ParsedFrame(
            speed_mps=speed_mps,
            gyro_z_rads=gyro_z_rads,
            run_mode=run_mode,
            checksum=frame_checksum,
            raw=bytes(raw),
        )


# ═══════════════════════════════════════════════════
# 串口读取器（后台线程）
# ═══════════════════════════════════════════════════

class SerialReader:
    """
    在守护线程中读取串口原始字节，通过回调函数分发解析完成的帧。

    特性:
      - 无限重连：串口断开后自动重试，不丢业务逻辑
      - 异常隔离：单帧解析失败不影响后续读取
      - 线程安全回调：回调在读取线程中同步调用
    """

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        *,
        on_frame: Optional[Callable[[ParsedFrame], None]] = None,
        reconnect_delay: float = 2.0,
    ):
        self.port = port
        self.baudrate = baudrate
        self._on_frame = on_frame
        self._reconnect_delay = reconnect_delay

        self._serial: Optional[serial.Serial] = None
        self._thread: Optional[threading.Thread] = None
        self._parser = STM32FrameParser()
        self._running = False

        # 统计计数器（无锁，仅读取线程写入）
        self._frame_count = 0
        self._error_count = 0

    # --------------------------------------------------
    # 公开 API
    # --------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def stats(self) -> dict:
        return {
            "port": self.port,
            "running": self._running,
            "frames_received": self._frame_count,
            "checksum_errors": self._error_count,
        }

    def set_frame_callback(self, callback: Callable[[ParsedFrame], None]) -> None:
        """运行时替换帧回调"""
        self._on_frame = callback

    def start(self) -> None:
        """启动串口读取守护线程"""
        if self._running:
            logger.warning("SerialReader is already running")
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._read_loop, daemon=True, name="serial-reader"
        )
        self._thread.start()
        logger.info(
            "SerialReader started — port=%s baud=%d", self.port, self.baudrate
        )

    def stop(self) -> None:
        """停止串口读取并等待线程退出"""
        self._running = False
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        self._close_serial()
        logger.info("SerialReader stopped — total frames=%d", self._frame_count)

    @staticmethod
    def list_ports() -> list[dict]:
        """枚举系统中所有可用的串口设备"""
        return [
            {
                "device": p.device,
                "name": p.name,
                "description": p.description,
                "hwid": p.hwid,
            }
            for p in serial.tools.list_ports.comports()
        ]

    # --------------------------------------------------
    # 内部：串口连接管理
    # --------------------------------------------------

    def _open_serial(self) -> bool:
        """尝试打开串口；成功返回 True，失败返回 False"""
        try:
            self._serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=1.0,  # read(1) 的超时，用于周期性检查 _running 标志
            )
            self._parser.reset()  # 清空半帧缓冲区
            logger.info("Serial port %s opened @ %d baud", self.port, self.baudrate)
            return True
        except serial.SerialException as exc:
            logger.error("Cannot open serial port %s: %s", self.port, exc)
            return False
        except Exception as exc:
            logger.exception("Unexpected error opening serial port: %s", exc)
            return False

    def _close_serial(self) -> None:
        """安全关闭串口"""
        ser = self._serial
        self._serial = None
        if ser is not None and ser.is_open:
            try:
                ser.close()
            except Exception:
                pass
            logger.debug("Serial port closed")

    # --------------------------------------------------
    # 内部：主读取循环
    # --------------------------------------------------

    def _read_loop(self) -> None:
        """
        守护线程主循环：
          1. 确保串口打开（失败则等待重连）
          2. 逐字节读取 → 喂入帧解析器
          3. 完整帧通过后调用回调
          4. 串口异常时自动重连
        """
        reconnect_delay = self._reconnect_delay

        while self._running:
            # --- 确保串口打开 ---
            if self._serial is None or not self._serial.is_open:
                if not self._open_serial():
                    self._sleep_interruptible(reconnect_delay)
                    continue

            # --- 读取 & 解析 ---
            try:
                raw_byte = self._serial.read(1)
                if not raw_byte:
                    # timeout — 无数据，继续轮询
                    continue

                frame = self._parser.feed(raw_byte[0])
                if frame is not None:
                    self._frame_count += 1
                    self._dispatch_frame(frame)

            except (serial.SerialException, OSError) as exc:
                logger.error(
                    "Serial I/O error on %s: %s — will reconnect in %.1fs",
                    self.port,
                    exc,
                    reconnect_delay,
                )
                self._error_count += 1
                self._close_serial()
                self._sleep_interruptible(reconnect_delay)

            except Exception:
                logger.exception("Unhandled exception in serial read loop")
                self._error_count += 1
                self._close_serial()
                self._sleep_interruptible(reconnect_delay)

    def _dispatch_frame(self, frame: ParsedFrame) -> None:
        """将解析好的帧交给回调"""
        logger.debug(
            "Frame #%d — speed=%.3f m/s  gyro_z=%.4f rad/s  mode=%d",
            self._frame_count,
            frame.speed_mps,
            frame.gyro_z_rads,
            frame.run_mode,
        )
        if self._on_frame is not None:
            try:
                self._on_frame(frame)
            except Exception:
                logger.exception("Unhandled exception in frame callback")

    def _sleep_interruptible(self, duration: float) -> None:
        """分段 sleep，每 0.1s 检查 _running，实现快速停止"""
        deadline = time.monotonic() + duration
        while self._running and time.monotonic() < deadline:
            time.sleep(min(0.1, deadline - time.monotonic()))


# ═══════════════════════════════════════════════════
# MQTT 发布器（异步协程）
# ═══════════════════════════════════════════════════

class MQTTPublisher:
    """
    异步 MQTT 发布器。

    使用内部 asyncio.Queue 桥接同步的串口回调线程与异步的 MQTT 发布协程。
    队列满时丢弃最旧的帧（背压保护），保证延迟不累积。
    """

    _MAX_QUEUE_SIZE = 1024

    def __init__(self, broker_host: str = "127.0.0.1", broker_port: int = 1883):
        self._broker_host = broker_host
        self._broker_port = broker_port
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=self._MAX_QUEUE_SIZE)
        self._task: Optional[asyncio.Task] = None

    # --------------------------------------------------
    # 线程安全的入队方法
    # --------------------------------------------------

    def enqueue(self, data: dict) -> None:
        """
        将遥测字典放入发布队列（线程安全）。

        当队列满时丢弃最旧帧，确保新数据总被发送。
        """
        try:
            self._queue.put_nowait(data)
        except asyncio.QueueFull:
            logger.warning(
                "MQTT publish queue is full (%d items); dropping oldest frame",
                self._MAX_QUEUE_SIZE,
            )
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(data)
            except Exception:
                pass

    # --------------------------------------------------
    # 异步生命周期
    # --------------------------------------------------

    async def start(self) -> None:
        """启动 MQTT 发布协程"""
        if self._task is not None and not self._task.done():
            logger.warning("MQTTPublisher already running")
            return
        self._task = asyncio.create_task(self._publish_loop(), name="mqtt-publisher")
        logger.info(
            "MQTTPublisher started — broker=%s:%d", self._broker_host, self._broker_port
        )

    async def stop(self) -> None:
        """停止 MQTT 发布"""
        task = self._task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info("MQTTPublisher stopped")

    # --------------------------------------------------
    # 发布循环
    # --------------------------------------------------

    async def _publish_loop(self) -> None:
        """从队列取数据 → JSON 序列化 → 发布到 vehicle/telemetry"""
        from aiomqtt import Client, MqttError

        while True:
            try:
                async with Client(self._broker_host, self._broker_port) as client:
                    logger.info(
                        "MQTT publisher connected to %s:%d",
                        self._broker_host,
                        self._broker_port,
                    )
                    # 内部循环：阻塞在 queue.get()，
                    # 若 MQTT 连接断开则跳出到外层重连
                    while True:
                        data = await self._queue.get()
                        try:
                            payload = json.dumps(data, ensure_ascii=False)
                            await client.publish(
                                "vehicle/telemetry", payload, qos=0
                            )
                            logger.debug(
                                "Published telemetry ts=%d",
                                data.get("timestamp", 0),
                            )
                        except Exception as exc:
                            logger.error("MQTT publish failed: %s", exc)
                            # 发布失败，将数据放回队首避免丢失
                            self._requeue(data)
                            raise  # 跳出内层循环触发重连

            except MqttError as exc:
                logger.error(
                    "MQTT connection error: %s — reconnecting in 3s...", exc
                )
                await asyncio.sleep(3.0)
            except asyncio.CancelledError:
                logger.info("MQTT publish loop cancelled")
                raise
            except Exception:
                logger.exception("MQTT publisher fatal error; reconnecting in 3s...")
                await asyncio.sleep(3.0)

    def _requeue(self, data: dict) -> None:
        """将数据放回队首（尽力而为）"""
        try:
            # asyncio.Queue 不支持 push_front，这里用 put_nowait
            # 如果满了则丢弃（原始帧已丢失，保证不阻塞）
            self._queue.put_nowait(data)
        except asyncio.QueueFull:
            logger.warning("Cannot requeue failed frame — queue full, dropping")


# ═══════════════════════════════════════════════════
# 顶层桥接器
# ═══════════════════════════════════════════════════

class STM32SerialBridge:
    """
    串口 → MQTT 一站式桥接器。

    串口线程  ──callback──>  MQTT 队列  ──async──>  MQTT Broker
                                            │
                                     vehicle/telemetry
                                            │
                                     MQTTBridge (mqtt_bridge.py)
                                            │
                               ┌────────────┼────────────┐
                               v            v            v
                          parse_packet   db_logger   WebSocket
                                             │        broadcast
                                          SQLite      前端
    """

    def __init__(
        self,
        port: str,
        *,
        baudrate: int = 115200,
        broker_host: str = "127.0.0.1",
        broker_port: int = 1883,
    ):
        self._publisher = MQTTPublisher(broker_host, broker_port)
        self._reader = SerialReader(
            port, baudrate, on_frame=self._on_serial_frame
        )
        self._running = False

    # --------------------------------------------------
    # 回调：串口线程 → 异步发布队列
    # --------------------------------------------------

    def _on_serial_frame(self, frame: ParsedFrame) -> None:
        """串口帧回调 — 将帧转为 JSON 字典并入队到 MQTT 发布队列"""
        telemetry_dict = frame.to_telemetry_dict()
        try:
            loop = asyncio.get_running_loop()
            # 从串口读取线程安全地调度到事件循环
            loop.call_soon_threadsafe(self._publisher.enqueue, telemetry_dict)
        except RuntimeError:
            # 事件循环未运行（不应在正常流程中出现）
            logger.error(
                "No running event loop — cannot enqueue telemetry frame"
            )

    # --------------------------------------------------
    # 生命周期
    # --------------------------------------------------

    async def start(self) -> None:
        """启动整个桥接：MQTT 发布协程 + 串口读取线程"""
        if self._running:
            return
        self._running = True
        await self._publisher.start()
        self._reader.start()
        logger.info("STM32SerialBridge started successfully")

    async def stop(self) -> None:
        """停止整个桥接"""
        self._running = False
        self._reader.stop()
        await self._publisher.stop()
        logger.info("STM32SerialBridge stopped")

    @property
    def stats(self) -> dict:
        return self._reader.stats


# ═══════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════

def _is_finite(value: float) -> bool:
    """检查浮点数是否为有限值（非 NaN / 非 Inf）"""
    import math
    return math.isfinite(value)


# ═══════════════════════════════════════════════════
# 独立运行入口
# ═══════════════════════════════════════════════════

async def _async_main() -> None:
    """独立脚本主函数"""
    import argparse

    ap = argparse.ArgumentParser(
        description="STM32 Serial → MQTT Telemetry Bridge",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python serial_reader.py -p COM3
  python serial_reader.py -p /dev/ttyUSB0 -b 921600 --broker 192.168.1.100
  python serial_reader.py --list-ports
        """.strip(),
    )
    ap.add_argument(
        "-p", "--port",
        help="串口设备路径 (例: COM3, /dev/ttyUSB0)",
    )
    ap.add_argument(
        "-b", "--baudrate",
        type=int,
        default=115200,
        help="波特率 (默认: 115200)",
    )
    ap.add_argument(
        "--broker",
        default="127.0.0.1",
        help="MQTT broker 地址 (默认: 127.0.0.1)",
    )
    ap.add_argument(
        "--broker-port",
        type=int,
        default=1883,
        help="MQTT broker 端口 (默认: 1883)",
    )
    ap.add_argument(
        "--list-ports",
        action="store_true",
        help="列出所有可用串口并退出",
    )
    ap.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="启用 DEBUG 级别日志",
    )
    args = ap.parse_args()

    # --- 列表模式 ---
    if args.list_ports:
        ports = SerialReader.list_ports()
        if not ports:
            print("未发现任何串口设备。")
        else:
            print("可用串口设备:")
            for p in ports:
                print(f"  {p['device']:<16} {p['description']:<32} {p['hwid']}")
        return

    # --- 运行模式需要指定端口 ---
    if not args.port:
        ap.error("需要指定串口: -p/--port COM3  (或用 --list-ports 查看可用串口)")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)-7s] %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    bridge = STM32SerialBridge(
        port=args.port,
        baudrate=args.baudrate,
        broker_host=args.broker,
        broker_port=args.broker_port,
    )

    logger.info("Starting STM32 Serial Bridge...")
    try:
        await bridge.start()
        logger.info("Bridge running — Ctrl+C to stop")
        # 保持运行，定期输出统计信息
        while True:
            await asyncio.sleep(30)
            s = bridge.stats
            logger.info(
                "Heartbeat — frames=%d errors=%d running=%s",
                s["frames_received"],
                s["checksum_errors"],
                s["running"],
            )
    except KeyboardInterrupt:
        logger.info("Received SIGINT, shutting down gracefully...")
    finally:
        await bridge.stop()
        logger.info("Bridge shutdown complete")


def main() -> None:
    """同步入口包装"""
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
