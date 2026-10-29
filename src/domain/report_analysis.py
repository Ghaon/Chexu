from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


Statement = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


class ReportAnalysisError(Exception):
    pass


class ReportReferenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    report_id: UUID
    chunk: int = Field(ge=1)
    quote: Annotated[str, StringConstraints(strip_whitespace=True, min_length=4, max_length=400)]


class ReportFindingInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    statement: Statement
    evidence: list[ReportReferenceInput] = Field(min_length=1, max_length=3)


class ReportAnalysisInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vehicle_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]
    facts: list[ReportFindingInput] = Field(default_factory=list, max_length=10)
    unknowns: list[ReportFindingInput] = Field(default_factory=list, max_length=10)
    follow_ups: list[ReportFindingInput] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def check_items(self) -> Self:
        count = len(self.facts) + len(self.unknowns) + len(self.follow_ups)
        if not 1 <= count <= 20:
            raise ValueError("解读结果需要 1～20 项带原文依据的内容。")
        return self


class VerifiedReportReference(ReportReferenceInput):
    filename: str
    sha256: str
    citation: str


class VerifiedReportFinding(BaseModel):
    statement: str
    evidence: list[VerifiedReportReference]


class ReportCoverage(BaseModel):
    report_id: UUID
    filename: str
    sha256: str
    read_chunks: list[int]
    total_chunks: int
    unread_chunks: int
    truncated: bool
    extracted_text_complete: bool


class VehicleReportAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vehicle_id: str
    facts: list[VerifiedReportFinding]
    unknowns: list[VerifiedReportFinding]
    follow_ups: list[VerifiedReportFinding]
    coverage: list[ReportCoverage]
    limitations: list[str]
    scope: Literal["extracted_report_text"] = "extracted_report_text"
    requires_human_review: Literal[True] = True
