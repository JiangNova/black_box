import asyncio
import json
import logging

from aiomqtt import Client

from database.db_logger import db_logger
from hardware.packet_parser import parse_packet
from services.websocket_manager import manager

logger = logging.getLogger("hardware.mqtt_bridge")

DEFAULT_BROKER = "127.0.0.1"
DEFAULT_PORT = 1883
TOPIC_TELEMETRY = "vehicle/telemetry"
TOPIC_CONTROL = "vehicle/control_cmd"
TOPIC_MODE_CMD = "vehicle/mode_cmd"
CONNECT_TIMEOUT_S = 15.0


class MQTTBridge:
    """纯异步 MQTT 数据桥梁：订阅遥测、发布控制。"""

    def __init__(self, broker: str = DEFAULT_BROKER, port: int = DEFAULT_PORT):
        self._broker = broker
        self._port = port
        self._task: asyncio.Task | None = None
        self._client: Client | None = None
        self._ready = asyncio.Event()

    async def start(self) -> None:
        if self._task and not self._task.done():
            logger.warning("MQTT bridge already running")
            return

        self._ready.clear()
        self._task = asyncio.create_task(self._run(), name="mqtt-bridge")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=CONNECT_TIMEOUT_S)
        except asyncio.TimeoutError:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            raise RuntimeError(
                f"MQTT bridge failed to connect to {self._broker}:{self._port} "
                f"within {CONNECT_TIMEOUT_S}s"
            )
        logger.info(
            "MQTT bridge started; subscribed to %s", TOPIC_TELEMETRY
        )

    async def stop(self) -> None:
        if self._task is None:
            return

        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
            self._client = None

        logger.info("MQTT bridge stopped")

    async def _run(self) -> None:
        try:
            async with Client(self._broker, self._port) as client:
                self._client = client
                await client.subscribe(TOPIC_TELEMETRY)
                self._ready.set()
                logger.info(
                    "Connected to MQTT broker %s:%s", self._broker, self._port
                )

                async for message in client.messages:
                    await self._handle_telemetry(message)

        except asyncio.CancelledError:
            logger.info("MQTT consumer loop cancelled")
            raise
        except Exception:
            logger.exception("MQTT bridge encountered a fatal error")
            raise

    async def _handle_telemetry(self, message) -> None:
        try:
            raw = message.payload.decode()
        except UnicodeDecodeError:
            logger.warning(
                "Non-UTF-8 telemetry payload on topic %s", message.topic
            )
            return

        valid_data = parse_packet(raw)
        if valid_data is None:
            return

        data_dict = valid_data.model_dump()
        await db_logger.insert_telemetry(data_dict)
        await manager.broadcast(data_dict)
        logger.debug(
            "Telemetry processed ts=%s run_mode=%s speed=%.3f",
            data_dict.get("timestamp"),
            data_dict.get("chassis", {}).get("run_mode"),
            data_dict.get("chassis", {}).get("speed_mps", 0.0),
        )

    async def publish_mode_cmd(self, mode: str) -> None:
        if self._client is None:
            logger.error("Cannot publish mode command; MQTT client is not connected")
            return

        body = json.dumps({"type": "mode_switch", "target": mode})
        await self._client.publish(TOPIC_MODE_CMD, body)
        logger.info("Published mode command to %s: %s", TOPIC_MODE_CMD, mode)

    async def publish_control_cmd(self, payload: dict) -> None:
        if self._client is None:
            logger.error(
                "Cannot publish control command; MQTT client is not connected"
            )
            return

        body = json.dumps(payload)
        await self._client.publish(TOPIC_CONTROL, body)
        logger.info("Published control command to %s: %s", TOPIC_CONTROL, body)


mqtt_bridge = MQTTBridge()
