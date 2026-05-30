from pydantic import BaseModel, Field


class ChassisData(BaseModel):
    speed_mps: float
    steer_angle_deg: float = 0.0


class ImuData(BaseModel):
    yaw: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0
    gyro_z_rads: float = 0.0


class LidarZones(BaseModel):
    front: float
    left: float
    right: float


class PerceptionData(BaseModel):
    lidar_zones_m: LidarZones
    lidar_360: list = Field(default_factory=list)


class TelemetryData(BaseModel):
    timestamp_us: int
    run_mode: str = "MANUAL"
    aeb_active: bool = False
    chassis: ChassisData
    imu: ImuData
    perception: PerceptionData
