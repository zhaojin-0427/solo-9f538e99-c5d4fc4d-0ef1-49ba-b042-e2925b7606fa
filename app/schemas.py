"""Request / response schemas.

Numeric inputs are *strings* on purpose: a JSON number is decoded to a binary
float before validation and can already be inexact (e.g. ``7.1``).  Taking the
raw text lets us parse decimals / fractions losslessly.  In ``/docs`` fill the
fields as ``"7.15"`` or ``"143/20"``.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, Field


def _as_exact_text(v: Any) -> str:
    """Coerce JSON values to lossless-ish decimal text.

    Strings are returned untouched (the exact path).  JSON integers are exact;
    JSON floats have already lost their source text by validation time, so we
    use Python's shortest round-trip ``repr`` which recovers the usual decimal
    literal intent.  Sending strings remains the recommended, fully exact way.
    """
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        raise ValueError("布尔值不是合法的数字/编号")
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if not math.isfinite(v):
            raise ValueError("数字必须有限")
        return repr(v)
    if isinstance(v, Decimal):
        return format(v, "f")
    raise ValueError(f"无法把 {type(v).__name__} 解析为十进制文本")


# OpenAPI still reports these as ``string``.
ExactText = Annotated[str, BeforeValidator(_as_exact_text)]
IdText = Annotated[str, BeforeValidator(_as_exact_text)]


class QuoteCreate(BaseModel):
    quote_no: IdText = Field(..., min_length=1, max_length=40,
                             description="报价号（唯一，按它登记/修改/撤回）")
    source: IdText = Field(..., description="原币代码，1 单位原币", examples=["USD"])
    target: IdText = Field(..., description="目标币代码", examples=["CNY"])
    lower: ExactText = Field(..., description="区间下界（含），正十进制或正分数", examples=["7.10"])
    upper: ExactText = Field(..., description="区间上界（含），正十进制或正分数", examples=["7.20"])

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "quote_no": "Q-USDCNY-001",
                    "source": "USD",
                    "target": "CNY",
                    "lower": "7.10",
                    "upper": "36/5",
                }
            ]
        }
    }


class QuoteUpdate(BaseModel):
    lower: ExactText = Field(..., description="新的区间下界（含）", examples=["7.05"])
    upper: ExactText = Field(..., description="新的区间上界（含）", examples=["7.15"])

    model_config = {
        "json_schema_extra": {
            "examples": [{"lower": "7.05", "upper": "7.15"}]
        }
    }


class OrderIn(BaseModel):
    order_no: IdText = Field(..., min_length=1, max_length=40, description="报销单号（全账簿唯一）")
    source: IdText = Field(..., description="原币（与目标币不同）", examples=["USD"])
    target: IdText = Field(..., description="申报折算币（与原币不同）", examples=["CNY"])
    original_amount: ExactText = Field(..., description="原币额，十进制正数，按原值核对", examples=["100.00"])
    declared_amount: ExactText = Field(..., description="申报折算额，十进制正数（目标币）", examples=["715.00"])

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "order_no": "BX-2026-0001",
                    "source": "USD",
                    "target": "CNY",
                    "original_amount": "100.00",
                    "declared_amount": "715.00",
                }
            ]
        }
    }


class BatchCreate(BaseModel):
    orders: list[OrderIn] = Field(..., min_length=1, description="同一批提交的报销单（整批成败）")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "orders": [
                        {
                            "order_no": "BX-2026-0001",
                            "source": "USD",
                            "target": "CNY",
                            "original_amount": "100.00",
                            "declared_amount": "710.00",
                        },
                        {
                            "order_no": "BX-2026-0002",
                            "source": "USD",
                            "target": "CNY",
                            "original_amount": "200.00",
                            "declared_amount": "1430.00",
                        },
                    ]
                }
            ]
        }
    }
