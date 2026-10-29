from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer


ReportSuffix = Literal[".pdf", ".docx", ".txt", ".md"]
REPORT_CHUNK_SIZE = 1600


class ReportError(Exception):
    pass


class ReportNotFound(ReportError):
    pass


class ReportFileError(ReportError):
    pass


class ReportTooLarge(ReportFileError):
    pass


class ReportReadError(ReportError):
    pass


class VehicleReport(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    report_id: UUID
    vehicle_id: str
    filename: str
    suffix: ReportSuffix
    size_bytes: int
    sha256: str
    uploaded_by_employee_id: UUID
    created_at: datetime

    @field_serializer("created_at", when_used="json")
    def serialize_created_at(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat()


class ReportList(BaseModel):
    reports: list[VehicleReport]
    has_more: bool


class ReportChunk(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    number: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=REPORT_CHUNK_SIZE)
    citation: str


class ParsedReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_id: UUID
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    parser: Literal["markitdown", "utf8-text"]
    chunks: list[ReportChunk]
    total_chunks: int = Field(ge=1)
    has_more: bool
    truncated: bool


class ReportEvidence(BaseModel):
    report_id: UUID
    vehicle_id: str
    filename: str
    sha256: str
    chunk: int = Field(ge=1)
    citation: str
    text: str = Field(min_length=1, max_length=REPORT_CHUNK_SIZE)


class VehicleReportReadResult(ParsedReport):
    vehicle_id: str
    filename: str
    provider: Literal["local_mcp"] = "local_mcp"
    untrusted_document: Literal[True] = True
