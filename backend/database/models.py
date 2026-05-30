"""
Tri-Channel Telemetry Protocol · 数据宪法 v2.0
─────────────────────────────────────────────
三大独立子模块，对应三路物理硬件：
  ChassisState — STM32 底盘层
  LidarState   — 激光雷达层
  VisionState  — OAK-D 视觉层
"""

from pydantic import BaseModel, Field


class ChassisState(BaseModel):
    """STM32 底盘层：运动学、姿态、运行模式与安全状态。"""
    speed_mps: float = 0.0
    gyro_z_rads: float = 0.0
    yaw: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0
    run_mode: str = "MANUAL"
    aeb_active: bool = False
    steer_angle_deg: float = 0.0


class LidarState(BaseModel):
    """激光雷达层：前向避障距离 + 360° 极坐标点云 (BEV)。"""
    front_m: float = 0.0
    lidar_360: list = Field(default_factory=list)


class VisionState(BaseModel):
    """OAK-D 视觉层：目标检测结果。"""
    has_obstacle: bool = False
    obstacle_label: str = ""
    distance_m: float = 0.0


class TelemetryFrame(BaseModel):
    """顶级遥测帧：组合三大通道 + 微秒时间戳。"""
    chassis: ChassisState = Field(default_factory=ChassisState)
    lidar: LidarState = Field(default_factory=LidarState)
    vision: VisionState = Field(default_factory=VisionState)
    timestamp: int = 0
