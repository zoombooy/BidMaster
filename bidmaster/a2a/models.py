"""A2A（Agent2Agent）协议数据模型。

遵循 A2A 规范（protocolVersion 0.2.x）的核心对象：
  AgentCard / AgentSkill / AgentCapabilities —— 能力发现
  Message / Part(text|file|data) / Task / TaskStatus / Artifact —— 任务交互

设计为对规范形状"宽松解析、严格输出"（extra=allow），保证与官方 SDK 及
LangChain/CrewAI/Coze 等第三方客户端互通。
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field

PROTOCOL_VERSION = "0.2.9"


# ---------------- Agent Card ----------------
class AgentProvider(BaseModel):
    organization: str
    url: str = ""


class AgentCapabilities(BaseModel):
    streaming: bool = True
    pushNotifications: bool = True
    stateTransitionHistory: bool = False


class AgentSkill(BaseModel):
    id: str
    name: str
    description: str
    tags: list[str] = []
    examples: list[str] = []
    inputModes: list[str] = []
    outputModes: list[str] = []


class AgentAuthentication(BaseModel):
    securitySchemes: dict = Field(default_factory=dict)
    security: list = Field(default_factory=list)


class AgentCard(BaseModel):
    name: str
    description: str
    url: str
    provider: Optional[AgentProvider] = None
    version: str
    protocolVersion: str = PROTOCOL_VERSION
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    authentication: Optional[AgentAuthentication] = None
    securitySchemes: dict = Field(default_factory=dict)
    security: list = Field(default_factory=list)
    defaultInputModes: list[str] = []
    defaultOutputModes: list[str] = []
    skills: list[AgentSkill] = []


# ---------------- Message / Part ----------------
class FileBlock(BaseModel):
    """FilePart 的 file 对象：bytes（base64）与 uri 二选一。"""
    name: Optional[str] = None
    mimeType: Optional[str] = None
    bytes: Optional[str] = None
    uri: Optional[str] = None


class Part(BaseModel):
    kind: Literal["text", "file", "data"]
    text: Optional[str] = None
    file: Optional[FileBlock] = None
    data: Optional[dict] = None
    metadata: Optional[dict] = None


class Message(BaseModel):
    role: str = "user"
    parts: list[Part] = []
    kind: Literal["message"] = "message"
    messageId: str = Field(default_factory=lambda: __import__("uuid").uuid4().hex)
    taskId: Optional[str] = None
    contextId: Optional[str] = None
    referenceTaskIds: list[str] = []
    metadata: Optional[dict] = None


# ---------------- Task ----------------
class TaskState(str, Enum):
    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    COMPLETED = "completed"
    CANCELED = "canceled"
    FAILED = "failed"
    REJECTED = "rejected"


class TaskStatus(BaseModel):
    state: TaskState
    message: Optional[Message] = None
    timestamp: str = ""


class Artifact(BaseModel):
    artifactId: str
    name: Optional[str] = None
    description: Optional[str] = None
    parts: list[Part] = []
    index: int = 0
    lastChunk: Optional[bool] = None
    metadata: Optional[dict] = None


class Task(BaseModel):
    id: str
    contextId: str = ""
    status: TaskStatus
    artifacts: list[Artifact] = []
    history: list[Message] = []
    kind: Literal["task"] = "task"
    metadata: Optional[dict] = None


# ---------------- 推送通知配置 ----------------
class PushNotificationConfig(BaseModel):
    url: str
    token: Optional[str] = None
    authentication: Optional[dict] = None


class TaskPushNotificationConfig(BaseModel):
    taskId: str
    pushNotificationConfig: PushNotificationConfig


# ---------------- JSON-RPC ----------------
class A2AError:
    TASK_NOT_FOUND = -32001
    TASK_NOT_CANCELABLE = -32002
    PUSH_NOT_SUPPORTED = -32003
    UNSUPPORTED_OPERATION = -32004
    CONTENT_TYPE_NOT_SUPPORTED = -32005
    INVALID_AGENT_RESPONSE = -32006


def rpc_error(code: int, message: str, rpc_id: Any = None) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id,
            "error": {"code": code, "message": message}}


def rpc_result(result: Any, rpc_id: Any = None) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}
