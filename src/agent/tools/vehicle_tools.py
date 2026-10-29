import logging
from typing import Annotated

from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from src.database.connection import SessionFactory
from src.database.models import VehicleRecord
from src.database.timeouts import apply_query_timeout


logger = logging.getLogger(__name__)


class VehicleToolError(Exception):
    pass


def vehicle_to_dict(vehicle: VehicleRecord) -> dict:
    return {
        "vehicle_id": vehicle.vehicle_id,
        "title": vehicle.title,
        "price_cny": vehicle.price_cny,
        "mileage_km": vehicle.mileage_km,
        "is_demo": vehicle.is_demo,
    }


def load_vehicle(vehicle_id: str, timeout_ms: int | None = None) -> dict:
    """读取车辆的当前资料，供查询和报价工具复用。"""

    vehicle_id = vehicle_id.strip()
    if not vehicle_id or len(vehicle_id) > 32 or "\x00" in vehicle_id:
        raise VehicleToolError("请提供有效的车辆编号（1～32 个字符）。")

    try:
        with SessionFactory() as session:
            apply_query_timeout(session, timeout_ms)
            vehicle = session.scalar(
                select(VehicleRecord).where(VehicleRecord.vehicle_id == vehicle_id)
            )
            if vehicle is None:
                raise VehicleToolError("未找到该编号对应的车辆，请核对车辆编号。")

            # 在 Session 关闭前提取工具所需字段，不向图中传递 ORM 对象。
            return vehicle_to_dict(vehicle)
    except SQLAlchemyError as exc:
        logger.warning("Vehicle lookup failed (%s).", type(exc).__name__)
        raise VehicleToolError("车辆数据库暂时不可用，请稍后重试。") from exc


@tool
def get_vehicle(vehicle_id: str, config: RunnableConfig) -> dict:
    """根据门店车辆编号查询车辆资料，包括价格、里程及已有信息。"""
    return load_vehicle(vehicle_id, config.get("configurable", {}).get("tool_timeout_ms"))


@tool
def search_vehicles(
    min_price_cny: Annotated[int | None, Field(strict=True, ge=0, le=2_147_483_647)] = None,
    max_price_cny: Annotated[int | None, Field(strict=True, ge=0, le=2_147_483_647)] = None,
    max_mileage_km: Annotated[int | None, Field(strict=True, ge=0, le=2_147_483_647)] = None,
    limit: Annotated[int, Field(strict=True, ge=1, le=20)] = 5,
    *, config: RunnableConfig,
) -> dict:
    """按价格区间和最高里程筛选库存，按价格从低到高返回。

    价格单位为整数元，里程单位为整数公里，所有边界均包含等于。
    未提供的条件不参与筛选；默认最多返回 5 辆，最多允许 20 辆。
    返回 vehicles 和 has_more；has_more 表示还有未展示的匹配车辆。
    """
    if min_price_cny is not None and max_price_cny is not None:
        if min_price_cny > max_price_cny:
            raise VehicleToolError("最低价格不能高于最高价格，请确认预算范围。")

    statement = select(VehicleRecord)
    if min_price_cny is not None:
        statement = statement.where(VehicleRecord.price_cny >= min_price_cny)
    if max_price_cny is not None:
        statement = statement.where(VehicleRecord.price_cny <= max_price_cny)
    if max_mileage_km is not None:
        statement = statement.where(VehicleRecord.mileage_km <= max_mileage_km)

    # 多取一条判断是否还有结果；同价时按内部 ID 排序，保证顺序稳定。
    statement = statement.order_by(VehicleRecord.price_cny, VehicleRecord.id).limit(limit + 1)
    try:
        with SessionFactory() as session:
            apply_query_timeout(session, config.get("configurable", {}).get("tool_timeout_ms"))
            vehicles = session.scalars(statement).all()
            return {
                "vehicles": [vehicle_to_dict(vehicle) for vehicle in vehicles[:limit]],
                "has_more": len(vehicles) > limit,
            }
    except SQLAlchemyError as exc:
        logger.warning("Vehicle search failed (%s).", type(exc).__name__)
        raise VehicleToolError("车辆数据库暂时不可用，请稍后重试。") from exc
