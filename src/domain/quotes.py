from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class QuoteDraft(BaseModel):
    """车辆报价快照；未确认费用不计入车辆报价金额。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["draft"] = "draft"
    vehicle_id: str
    vehicle_title: str
    currency: Literal["CNY"] = "CNY"
    listed_price_cny: int = Field(strict=True, ge=0)
    discount_cny: int = Field(strict=True, ge=0)
    quoted_price_cny: int = Field(strict=True, ge=0)
    pending_items: list[str] = Field(default_factory=lambda: [
        "税费", "保险费用", "过户或上牌费用", "其他费用（如有）",
    ])
    is_total_price: Literal[False] = False
    requires_confirmation: Literal[True] = True
    is_demo: bool
    generated_at: datetime


class QuoteCalculationError(ValueError):
    pass


def build_quote_draft(*, vehicle_id: str, vehicle_title: str, listed_price_cny: int,
                      discount_cny: int, is_demo: bool) -> QuoteDraft:
    if discount_cny < 0 or discount_cny > listed_price_cny:
        raise QuoteCalculationError("优惠金额必须为非负数，且不能超过当前车辆售价。")
    return QuoteDraft(
        vehicle_id=vehicle_id,
        vehicle_title=vehicle_title,
        listed_price_cny=listed_price_cny,
        discount_cny=discount_cny,
        quoted_price_cny=listed_price_cny - discount_cny,
        is_demo=is_demo,
        generated_at=datetime.now(timezone.utc),
    )
