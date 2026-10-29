from typing import Annotated

from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from src.agent.tools.vehicle_tools import VehicleToolError, load_vehicle
from src.domain.quotes import QuoteCalculationError, build_quote_draft


class QuoteDraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vehicle_id: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=32, pattern=r"^[^\x00]+$"),
    ]
    discount_cny: int = Field(
        default=0, strict=True, ge=0, le=2_147_483_647,
        description="用户明确指定的优惠金额，单位为整数元；未指定时使用 0。",
    )


def prepare_quote_draft(vehicle_id: str, discount_cny: int, timeout_ms: int | None = None) -> dict:
    vehicle = load_vehicle(vehicle_id, timeout_ms)
    try:
        draft = build_quote_draft(
            vehicle_id=vehicle["vehicle_id"], vehicle_title=vehicle["title"],
            listed_price_cny=vehicle["price_cny"], discount_cny=discount_cny,
            is_demo=vehicle["is_demo"],
        )
    except QuoteCalculationError as exc:
        raise VehicleToolError(str(exc)) from exc
    # JSON 模式将时间转换为可供工具消息和接口传递的字符串。
    return draft.model_dump(mode="json")


@tool(args_schema=QuoteDraftInput)
def create_quote_draft(vehicle_id: str, discount_cny: int = 0, *, config: RunnableConfig) -> dict:
    """查询最新车辆售价并预览报价草稿，不保存或确认报价。

    优惠只使用用户明确指定的整数元金额，未指定时为 0。
    未确认的税费等不计入车辆报价；价格和合计由工具读取与计算。
    """
    return prepare_quote_draft(vehicle_id, discount_cny, config.get("configurable", {}).get("tool_timeout_ms"))


@tool(args_schema=QuoteDraftInput)
def request_quote_approval(vehicle_id: str, discount_cny: int = 0, *, config: RunnableConfig) -> dict:
    """用户明确要求提交审批或保存确认报价时，准备报价并请求经理审核。

    一次单独提交一辆车，不能与其他工具并行调用。优惠只用用户明确指定的整数元金额。
    后续流程会暂停等待经理审核；此工具返回不表示报价已保存、已确认。
    单纯预览报价应使用 create_quote_draft，不要自行提交审批。
    """
    return prepare_quote_draft(vehicle_id, discount_cny, config.get("configurable", {}).get("tool_timeout_ms"))
