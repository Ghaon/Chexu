from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.domain.approvals import ApprovalTask
from src.domain.planning import ExecutionSkill
from src.domain.quotes import QuoteDraft
from src.domain.report_analysis import VehicleReportAnalysis
from src.domain.reports import ReportEvidence
from src.domain.web_search import WebSource, WebSearchStatus


class AgentVehicle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vehicle_id: str
    title: str
    price_cny: int
    mileage_km: int
    is_demo: bool


class AgentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reply: str = Field(min_length=1, max_length=30000)
    vehicle: AgentVehicle | None = None
    vehicles: list[AgentVehicle] = Field(default_factory=list, max_length=20)
    has_more: bool = False
    quote_draft: QuoteDraft | None = None
    approval: ApprovalTask | None = None
    quote_id: UUID | None = None
    report_sources: list[ReportEvidence] = Field(default_factory=list, max_length=30)
    report_analysis: VehicleReportAnalysis | None = None
    web_sources: list[WebSource] = Field(default_factory=list, max_length=10)
    web_search_status: WebSearchStatus | None = None


class SubagentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: UUID
    step_id: str
    skill_id: ExecutionSkill
    status: Literal["completed", "needs_input", "blocked"]
    output: AgentOutput


class SubagentContextExceeded(Exception):
    pass
