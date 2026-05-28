import logging

from hardware.mqtt_bridge import mqtt_bridge

logger = logging.getLogger("services.control_mux")


class ControlMultiplexer:
    def __init__(self):
        self.estop_latched = False
        self.active_source = "WEB"

    async def submit(self, source: str, action: str):
        """所有控制源必须通过此方法提交指令，由 MUX 统一仲裁。"""
        if action == "E_STOP":
            self.estop_latched = True
            logger.critical(
                "E-STOP received from %s; system latched", source
            )
            await self._send_to_hardware("E_STOP")
            return

        if self.estop_latched:
            logger.warning(
                "Command discarded while estop latched: source=%s action=%s",
                source,
                action,
            )
            return

        if source != self.active_source:
            pass

        logger.info(
            "Control granted to %s; dispatching action=%s", source, action
        )
        await self._send_to_hardware(action)

    async def _send_to_hardware(self, action: str):
        await mqtt_bridge.publish_control_cmd(
            {"type": "control", "action": action}
        )

    def reset_estop(self):
        self.estop_latched = False
        logger.info("E-stop latch cleared; control restored")


mux_commander = ControlMultiplexer()
