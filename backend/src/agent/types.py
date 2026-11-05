from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class MessageModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TextContent(MessageModel):
    type: Literal["text"] = "text"
    text: str


class ToolCallContent(MessageModel):
    type: Literal["toolCall"] = "toolCall"
    id: str
    name: str
    arguments: str


ContentBlock = Annotated[TextContent | ToolCallContent, Field(discriminator="type")]


class SystemMessage(MessageModel):
    role: Literal["system"] = "system"
    content: str


class UserMessage(MessageModel):
    role: Literal["user"] = "user"
    content: str


class AssistantMessage(MessageModel):
    role: Literal["assistant"] = "assistant"
    content: list[ContentBlock]


class ToolResultMessage(MessageModel):
    role: Literal["toolResult"] = "toolResult"
    tool_call_id: str
    content: str


class CustomMessage(MessageModel):
    role: Literal["custom"] = "custom"
    type: str
    data: JsonValue


Message = Annotated[
    SystemMessage | UserMessage | AssistantMessage | ToolResultMessage,
    Field(discriminator="role"),
]
AgentMessage = Annotated[
    SystemMessage | UserMessage | AssistantMessage | ToolResultMessage | CustomMessage,
    Field(discriminator="role"),
]
