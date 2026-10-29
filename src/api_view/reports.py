import logging
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import JSONResponse

from src.api_view.auth import get_current_employee
from src.api_view.schemas import EmployeeResponse
from src.domain.reports import (ParsedReport, ReportFileError, ReportList, ReportNotFound,
                                ReportReadError, ReportTooLarge, VehicleReport)
from src.services import reports


logger = logging.getLogger(__name__)
router = APIRouter(tags=["vehicle-reports"])


class ReportUploadLimitMiddleware:
    """在 multipart 解析和临时文件落盘之前限制整次报告上传大小。"""

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes + 65536

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if (scope["type"] != "http" or scope.get("method") != "POST"
                or not path.startswith("/api/vehicles/") or not path.endswith("/reports")):
            return await self.app(scope, receive, send)
        messages, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > self.max_bytes:
                return await JSONResponse(status_code=413, content={"detail": "报告上传超过大小限制。"})(scope, receive, send)
            messages.append(message)
            if not message.get("more_body", False):
                break
        iterator = iter(messages)

        async def replay():
            message = next(iterator, None)
            return message if message is not None else await receive()

        await self.app(scope, replay, send)


def report_error(error: Exception) -> HTTPException:
    if isinstance(error, ReportNotFound):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, ReportTooLarge):
        return HTTPException(status_code=413, detail=str(error))
    if isinstance(error, ReportFileError):
        return HTTPException(status_code=422, detail=str(error))
    if isinstance(error, ReportReadError):
        return HTTPException(status_code=502, detail=str(error))
    logger.warning("Vehicle report operation failed (%s).", type(error).__name__)
    return HTTPException(status_code=503, detail="车辆报告服务暂时不可用，请稍后重试。")


@router.post("/api/vehicles/{vehicle_id}/reports", response_model=VehicleReport, status_code=201)
def upload_report(vehicle_id: str, response: Response, file: UploadFile = File(...),
                   employee: EmployeeResponse = Depends(get_current_employee)):
    try:
        metadata, created = reports.upload_report(vehicle_id, employee.employee_id, file.filename, file.file)
        response.status_code = 201 if created else 200
        return metadata
    except (ReportNotFound, ReportFileError, SQLAlchemyError, OSError) as exc:
        raise report_error(exc) from exc


@router.get("/api/vehicles/{vehicle_id}/reports", response_model=ReportList)
def list_reports(vehicle_id: str, limit: int = Query(default=20, ge=1, le=100),
                  offset: int = Query(default=0, ge=0, le=100000),
                  employee: EmployeeResponse = Depends(get_current_employee)):
    try:
        return reports.list_reports(vehicle_id, limit=limit, offset=offset)
    except (ReportNotFound, SQLAlchemyError) as exc:
        raise report_error(exc) from exc


@router.get("/api/reports/{report_id}", response_model=VehicleReport)
def get_report(report_id: UUID, employee: EmployeeResponse = Depends(get_current_employee)):
    try:
        return reports.get_report(report_id)
    except (ReportNotFound, SQLAlchemyError) as exc:
        raise report_error(exc) from exc


@router.get("/api/reports/{report_id}/content", response_model=ParsedReport)
def read_report(report_id: UUID, start_chunk: int = Query(default=1, ge=1),
                 limit: int = Query(default=3, ge=1, le=5),
                 employee: EmployeeResponse = Depends(get_current_employee)):
    try:
        _, result = reports.read_report(report_id, start_chunk=start_chunk, limit=limit)
        return result
    except (ReportNotFound, ReportReadError, SQLAlchemyError, OSError) as exc:
        raise report_error(exc) from exc
