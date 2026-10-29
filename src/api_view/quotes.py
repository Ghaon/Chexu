import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import OperationalError, SQLAlchemyError, TimeoutError

from src.api_view.auth import get_current_employee, require_quote_confirmer
from src.api_view.schemas import EmployeeResponse, QuoteConfirm, QuoteSave, QuoteSaved
from src.database.connection import SessionFactory
from src.database.models import EmployeeRecord, QuoteRecord, VehicleRecord
from src.domain.quotes import QuoteCalculationError, build_quote_draft


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/quotes", tags=["quotes"])


@router.post("", response_model=QuoteSaved, status_code=201)
def save_quote(body: QuoteSave, employee: EmployeeResponse = Depends(get_current_employee)) -> QuoteSaved:
    try:
        with SessionFactory.begin() as session:
            actor = session.get(EmployeeRecord, employee.employee_id, with_for_update=True)
            if actor is None or not actor.is_active:
                raise HTTPException(status_code=401, detail="员工账号已停用或不存在。")
            # 核对价格与保存快照在同一事务内完成，防止期间库存被并发改价。
            vehicle = session.scalar(
                select(VehicleRecord).where(VehicleRecord.vehicle_id == body.vehicle_id).with_for_update()
            )
            if vehicle is None:
                raise HTTPException(status_code=404, detail="未找到该编号对应的车辆。")
            if vehicle.price_cny != body.expected_price_cny:
                raise HTTPException(status_code=409, detail="车辆售价已变化，请重新生成报价草稿后保存。")
            draft = build_quote_draft(
                vehicle_id=vehicle.vehicle_id, vehicle_title=vehicle.title,
                listed_price_cny=vehicle.price_cny, discount_cny=body.discount_cny,
                is_demo=vehicle.is_demo,
            )
            record = QuoteRecord(vehicle_record_id=vehicle.id, created_by_employee_id=actor.employee_id,
                                  **draft.model_dump())
            session.add(record)
            session.flush()
            response = QuoteSaved.model_validate(record)
        return response
    except QuoteCalculationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (OperationalError, TimeoutError) as exc:
        logger.warning("Quote storage unavailable (%s).", type(exc).__name__)
        raise HTTPException(status_code=503, detail="报价存储暂时不可用，请稍后重试。") from exc
    except SQLAlchemyError as exc:
        logger.error("Quote save failed (%s).", type(exc).__name__)
        raise HTTPException(status_code=500, detail="报价保存失败，请检查服务端日志。") from exc


@router.get("/{quote_id}", response_model=QuoteSaved)
def get_quote(quote_id: UUID, employee: EmployeeResponse = Depends(get_current_employee)) -> QuoteSaved:
    try:
        with SessionFactory() as session:
            record = session.get(QuoteRecord, quote_id)
            if record is None or (employee.role != "manager" and record.created_by_employee_id != employee.employee_id):
                raise HTTPException(status_code=404, detail="未找到该报价。")
            return QuoteSaved.model_validate(record)
    except (OperationalError, TimeoutError) as exc:
        logger.warning("Quote storage unavailable (%s).", type(exc).__name__)
        raise HTTPException(status_code=503, detail="报价存储暂时不可用，请稍后重试。") from exc
    except SQLAlchemyError as exc:
        logger.error("Quote lookup failed (%s).", type(exc).__name__)
        raise HTTPException(status_code=500, detail="报价查询失败，请检查服务端日志。") from exc


@router.post("/{quote_id}/confirm", response_model=QuoteSaved)
def confirm_quote(quote_id: UUID, body: QuoteConfirm,
                  employee: EmployeeResponse = Depends(require_quote_confirmer)) -> QuoteSaved:
    """显式确认车辆报价；待确认的其他费用仍不包含在报价金额中。"""
    try:
        with SessionFactory.begin() as session:
            # 在确认事务中复核并锁定员工，避免核对后角色或启用状态被并发修改。
            actor = session.get(EmployeeRecord, employee.employee_id, with_for_update=True)
            if actor is None or not actor.is_active or actor.role != "manager":
                raise HTTPException(status_code=403, detail="当前员工没有报价确认权限。")
            record = session.scalar(
                select(QuoteRecord).where(QuoteRecord.quote_id == quote_id).with_for_update()
            )
            if record is None:
                raise HTTPException(status_code=404, detail="未找到该报价。")
            # 锁定后检查状态，重复或并发确认返回同一份已确认结果。
            if record.status == "draft":
                vehicle = session.scalar(
                    select(VehicleRecord).where(VehicleRecord.id == record.vehicle_record_id).with_for_update()
                )
                if vehicle is None or vehicle.price_cny != record.listed_price_cny:
                    raise HTTPException(status_code=409, detail="车辆售价已变化，请重新生成并保存报价草稿后确认。")
                record.status = "confirmed"
                record.requires_confirmation = False
                record.confirmed_at = datetime.now(timezone.utc)
                record.confirmed_by_employee_id = actor.employee_id
                session.flush()
            response = QuoteSaved.model_validate(record)
        return response
    except (OperationalError, TimeoutError) as exc:
        logger.warning("Quote confirmation storage unavailable (%s).", type(exc).__name__)
        raise HTTPException(status_code=503, detail="报价存储暂时不可用，请稍后重试。") from exc
    except SQLAlchemyError as exc:
        logger.error("Quote confirmation failed (%s).", type(exc).__name__)
        raise HTTPException(status_code=500, detail="报价确认失败，请检查服务端日志。") from exc
