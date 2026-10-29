from typing import BinaryIO
from time import monotonic
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.agent.config import get_settings
from src.database.connection import SessionFactory
from src.database.models import EmployeeRecord, VehicleRecord, VehicleReportRecord
from src.database.timeouts import apply_query_timeout
from src.domain.reports import (ParsedReport, ReportFileError, ReportList, ReportNotFound, VehicleReport)
from src.mcp.report_client import read_report_via_mcp
from src.reports.storage import prepare_storage, report_path, stage_upload, validate_filename


def get_report(report_id: UUID, vehicle_id: str | None = None, timeout_ms: int | None = None) -> VehicleReport:
    with SessionFactory() as session:
        apply_query_timeout(session, timeout_ms)
        record = session.get(VehicleReportRecord, report_id)
        if record is None or (vehicle_id is not None and record.vehicle_id != vehicle_id):
            raise ReportNotFound("未找到该车辆对应的报告。")
        return VehicleReport.model_validate(record)


def list_reports(vehicle_id: str, *, limit: int = 20, offset: int = 0,
                  timeout_ms: int | None = None) -> ReportList:
    with SessionFactory() as session:
        apply_query_timeout(session, timeout_ms)
        if session.scalar(select(VehicleRecord.id).where(VehicleRecord.vehicle_id == vehicle_id)) is None:
            raise ReportNotFound("未找到该车辆。")
        records = session.scalars(select(VehicleReportRecord).where(VehicleReportRecord.vehicle_id == vehicle_id)
                                  .order_by(VehicleReportRecord.created_at.desc(), VehicleReportRecord.report_id)
                                  .offset(offset).limit(limit + 1)).all()
        return ReportList(reports=[VehicleReport.model_validate(row) for row in records[:limit]], has_more=len(records) > limit)


def upload_report(vehicle_id: str, employee_id: UUID, filename: str | None, source: BinaryIO) -> tuple[VehicleReport, bool]:
    filename, suffix = validate_filename(filename)
    settings = get_settings()
    root = prepare_storage(settings.report_storage_dir)
    # 先确认车辆存在，不为无效编号写入上传文件。
    list_reports(vehicle_id, limit=1)
    temporary, size, digest = stage_upload(root, source, suffix, settings.max_report_bytes)
    try:
        with SessionFactory.begin() as session:
            apply_query_timeout(session)
            employee = session.get(EmployeeRecord, employee_id, with_for_update=True)
            if employee is None or not employee.is_active:
                raise ReportFileError("当前员工已不可用，无法上传报告。")
            vehicle = session.scalar(select(VehicleRecord).where(VehicleRecord.vehicle_id == vehicle_id))
            if vehicle is None:
                raise ReportNotFound("未找到该车辆。")
            existing = session.scalar(select(VehicleReportRecord).where(
                VehicleReportRecord.vehicle_record_id == vehicle.id, VehicleReportRecord.sha256 == digest,
            ))
            if existing is not None:
                return VehicleReport.model_validate(existing), False
            record = VehicleReportRecord(report_id=uuid4(), vehicle_record_id=vehicle.id, vehicle_id=vehicle.vehicle_id,
                                          uploaded_by_employee_id=employee_id, filename=filename, suffix=suffix,
                                          size_bytes=size, sha256=digest)
            session.add(record)
            session.flush()
            temporary.replace(report_path(root, record.report_id, suffix))
            result = VehicleReport.model_validate(record)
        return result, True
    except IntegrityError as exc:
        name = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        if name != "uq_vehicle_reports_content":
            raise
        with SessionFactory() as session:
            record = session.scalar(select(VehicleReportRecord).where(
                VehicleReportRecord.vehicle_id == vehicle_id, VehicleReportRecord.sha256 == digest,
            ))
            if record is None:
                raise
            return VehicleReport.model_validate(record), False
    finally:
        # 最终文件在提交结果不明时保留，避免数据库已提交但文件被误删。
        temporary.unlink(missing_ok=True)


def read_report(report_id: UUID, *, vehicle_id: str | None = None, start_chunk: int = 1, limit: int = 3,
                 timeout_seconds: float | None = None, timeout_ms: int | None = None, cancel_check=None,
                 deadline: float | None = None) -> tuple[VehicleReport, ParsedReport]:
    metadata = get_report(report_id, vehicle_id, timeout_ms)
    settings = get_settings()
    timeout = timeout_seconds if timeout_seconds is not None else settings.report_mcp_timeout_seconds
    if deadline is not None:
        timeout = min(timeout, deadline - monotonic())
    result = read_report_via_mcp(root=settings.report_storage_dir, report_id=report_id, suffix=metadata.suffix,
                                  digest=metadata.sha256, start_chunk=start_chunk, limit=limit,
                                  max_bytes=settings.max_report_bytes, max_text_chars=settings.max_report_text_chars,
                                  timeout_seconds=timeout,
                                  cancel_check=cancel_check)
    return metadata, result
