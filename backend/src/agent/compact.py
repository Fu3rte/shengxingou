import json

from pydantic import BaseModel, ConfigDict

from .types import SystemMessage, ToolResultMessage, UserMessage
from ..ai.models.openai import to_openai_messages


class Summary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    current_goal: str
    user_preferences: list[str]
    task_progress: str
    completed_tasks: list[str]
    next_tasks: list[str]
    decisions: list[str]
    relevant_facts: list[str]
    open_questions: list[str]


OUTPUT_TOKENS = 4000
CONTEXT_TOKENS = 20000


PROMPT = (
    "将对话整理为 JSON 对象，仅记录原始对话明确支持且对后续任务有用的信息；"
    "保留关键约束、用户偏好、已作决策、任务状态和未解决问题，不推测、不补造。"
    "未知字段使用空字符串或空数组。严格输出 JSON，不附加 Markdown 或解释。"
    "字段：current_goal（字符串）、user_preferences（字符串数组）、task_progress（字符串）、"
    "completed_tasks、next_tasks、decisions、relevant_facts、open_questions（均为字符串数组）。"
)


def size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def estimate_tokens(messages: list, tools: list) -> float:
    return (size(messages) + size(tools)) / 4


def summary_message(summary: dict) -> SystemMessage:
    return SystemMessage(
        content="历史对话摘要：" + json.dumps(summary, ensure_ascii=False)
    )


def split_recent(records):
    """Return the older prefix, preserving complete assistant/tool groups and latest user."""
    groups = []
    for record in records:
        if isinstance(record.message, ToolResultMessage) and groups:
            groups[-1].append(record)
        else:
            groups.append([record])
    latest_user = next(
        (
            i
            for i in range(len(groups) - 1, -1, -1)
            if isinstance(groups[i][0].message, UserMessage)
        ),
        -1,
    )
    boundary = len(groups)
    total = 0
    while boundary and (total < 16000 or boundary > latest_user >= 0):
        boundary -= 1
        total += size(to_openai_messages([r.message for r in groups[boundary]]))
    return [r for group in groups[:boundary] for r in group]
