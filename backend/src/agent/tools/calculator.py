import json
import operator
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Calculation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    left: str = Field(description="十进制数字字符串")
    operator: Literal["+", "-", "*", "/"]
    right: str = Field(description="十进制数字字符串")


def calculate(arguments: str) -> str:
    calculation = Calculation.model_validate_json(arguments)
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


TOOLS = [{
    "type": "function",
    "function": {
        "name": "calculate",
        "description": "计算两个数字的加、减、乘或除。",
        "parameters": Calculation.model_json_schema(),
    },
}]
