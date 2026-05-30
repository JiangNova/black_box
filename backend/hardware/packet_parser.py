import json
import logging
from typing import Any

from pydantic import ValidationError

from database.models import TelemetryFrame

logger = logging.getLogger("hardware.packet_parser")


def parse_packet(data: str | dict[str, Any]) -> TelemetryFrame | None:
    """
    纯校验器：接收完整 JSON 字符串或字典，返回通过 Pydantic 校验的 TelemetryFrame。
    仅当 payload 严格符合三通道嵌套格式 (chassis/lidar/vision + timestamp) 时才放行。
    """
    try:
        if isinstance(data, str):
            data_dict = json.loads(data)
        else:
            data_dict = data

        return TelemetryFrame(**data_dict)

    except json.JSONDecodeError:
        logger.warning("JSON decode failed for telemetry payload: %r", data)
        return None
    except ValidationError as exc:
        logger.error("Telemetry validation failed: %s", exc)
        return None
    except Exception:
        logger.exception("Unexpected error while parsing telemetry")
        return None
