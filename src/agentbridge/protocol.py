from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SshTarget(StrictModel):
    host: str = Field(min_length=1)
    port: int = Field(default=22, ge=1, le=65535, strict=True)
    user: str = Field(min_length=1)
    password: SecretStr | None = None
    known_hosts: Path


class WorkspaceSpec(StrictModel):
    schema_version: Literal[1] = 1
    trace_id: str
    task_id: str | None = None
    run_id: str = ""
    flow: str = ""
    role: str = ""
    label: str = ""
    call_id: str = ""
    attempt: int = Field(default=1, ge=1)
    pool: str = ""
    resource: str = ""
    config_revision: str = ""
    execution_id: str
    node_id: str
    control_revision: int = Field(ge=0)
    machine_id: str
    client_id: str
    workspace_id: str
    cwd: Path
    read_roots: list[Path]
    write_roots: list[Path] = Field(default_factory=list)
    log_dir: Path
    access: Literal["read", "full"]
    @field_validator("execution_id", "trace_id")
    @classmethod
    def uuid_value(cls, value: str) -> str:
        return str(UUID(value))

    @model_validator(mode="after")
    def valid_runtime(self):
        if not self.cwd.is_absolute() or not self.log_dir.is_absolute():
            raise ValueError("cwd/log_dir 必须是绝对路径")
        if self.access == "read" and (self.write_roots or self.callbacks):
            raise ValueError("只读执行不能授权写目录或业务操作")
        return self

    def public_context(self) -> dict:
        fields = {"trace_id", "task_id", "run_id", "flow", "role", "label", "call_id", "attempt", "pool",
                  "resource", "config_revision", "execution_id", "node_id", "control_revision", "machine_id",
                  "client_id", "workspace_id", "session_id"}
        return self.model_dump(mode="json", include=fields)

    def secrets(self) -> list[bytes]:
        return []


class RunSpec(WorkspaceSpec):
    mode: Literal["print", "interactive"]
    target: SshTarget
    claude_exe: list[str] = Field(default_factory=lambda: ["claude"])
    model: str = ""
    effort: str = ""
    prompt: str = ""
    session_id: str | None = None
    resume: bool = False
    result_schema: dict | None = None
    callbacks: dict[str, str] = Field(default_factory=dict)
    callback_token: SecretStr | None = None


    @model_validator(mode="after")
    def valid_ssh(self):
        if self.resume and not self.session_id:
            raise ValueError("续接必须指定 session_id")
        if not self.claude_exe or not self.claude_exe[0] or any("\x00" in v for v in self.claude_exe):
            raise ValueError("Claude 命令无效")
        return self

    def secrets(self) -> list[bytes]:
        return [v.get_secret_value().encode() for v in (self.target.password, self.callback_token) if v]


class ToolsSpec(WorkspaceSpec):
    mode: Literal["tools"] = "tools"
    callbacks: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_tools(self):
        if self.callbacks:
            raise ValueError("工具服务的业务回执由宿主管理")
        return self


class ToolMeta(StrictModel):
    execution_id: str
    tool_call_id: str
    op_id: str | None = None
    control_revision: int


class ToolResult(StrictModel):
    status: Literal["ok", "running", "error", "cancelled", "outcome_unknown"]
    data: dict = Field(default_factory=dict)
    error_code: str | None = None
    artifact_refs: list[str] = Field(default_factory=list)
