import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError, TimeoutError

from src.api_view.schemas import VehicleCreate, VehicleCreated
from src.api_view.auth import get_current_employee
from src.database.connection import SessionFactory
from src.database.models import VehicleRecord


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/vehicles", tags=["vehicles"], dependencies=[Depends(get_current_employee)])


@router.post("", response_model=VehicleCreated, status_code=status.HTTP_201_CREATED)
def create_vehicle(body: VehicleCreate) -> VehicleCreated:
    try:
        with SessionFactory.begin() as session:
            vehicle = VehicleRecord(**body.model_dump())
            session.add(vehicle)
            session.flush()
            # 在会话关闭前读取数据库生成的字段，成功提交后才返回响应。
            response = VehicleCreated.model_validate(vehicle)
    except IntegrityError as exc:
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        if constraint == "uq_vehicles_vehicle_id":
            raise HTTPException(status_code=409, detail="该车辆编号已存在。") from exc
        logger.error("Vehicle insert failed integrity checks (%s).", type(exc.orig).__name__)
        raise HTTPException(status_code=500, detail="车辆保存失败，请检查服务端日志。") from exc
    except (OperationalError, TimeoutError) as exc:
        logger.warning("Vehicle database unavailable (%s).", type(exc).__name__)
        raise HTTPException(status_code=503, detail="数据库暂时不可用，请稍后重试。") from exc
    except SQLAlchemyError as exc:
        logger.error("Vehicle database operation failed (%s).", type(exc).__name__)
        raise HTTPException(status_code=500, detail="车辆保存失败，请检查服务端日志。") from exc

    return response
