import json
import operator
from collections.abc import Callable
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Calculation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    left: str = Field(description="十进制数字字符串")
    operator: Literal["+", "-", "*", "/"]
    right: str = Field(description="十进制数字字符串")


def calculate(
    calculation: Calculation, *, start: Callable[[], bool] | None = None
) -> str | None:
    if start is not None and not start():
        return None
    operations = {
        "+": operator.add,
        "-": operator.sub,
        "*": operator.mul,
        "/": operator.truediv,
    }
    left, right = Decimal(calculation.left), Decimal(calculation.right)
    if not left.is_finite() or not right.is_finite():
        raise ValueError("计算器只接受有限数字")
    result = operations[calculation.operator](left, right)
    return json.dumps({"result": str(result)})
