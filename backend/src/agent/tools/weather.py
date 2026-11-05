"""自定义天气查询：城市名到模拟天气记录的配置映射，无实时含义。"""

import json
from typing import TypedDict

from pydantic import BaseModel, ConfigDict, Field


class WeatherRecord(TypedDict):
    condition: str
    temperature_c: int


WEATHER_RECORDS: dict[str, WeatherRecord] = {
    "示例城": {"condition": "晴", "temperature_c": 24},
    "测试市": {"condition": "多云", "temperature_c": 19},
    "样例县": {"condition": "小雨", "temperature_c": 16},
}


class WeatherQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    location: str = Field(min_length=1, description="城市名")


def weather(arguments: str) -> str:
    request = WeatherQuery.model_validate_json(arguments)
    location = request.location.strip()
    if not location:
        raise ValueError("地点不能为空")
    record = WEATHER_RECORDS.get(location)
    if record is None:
        raise ValueError(f"没有该地点的天气记录: {location}")
    return json.dumps(
        {
            "location": location,
            "condition": record["condition"],
            "temperature_c": record["temperature_c"],
            "source": "custom",
            "is_live": False,
        },
        ensure_ascii=False,
    )
