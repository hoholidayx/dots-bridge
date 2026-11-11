from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ArtifactId = Annotated[str, Field(pattern=r"^art_[0-9a-f]{32}$")]
ArtifactIds = Annotated[list[ArtifactId], Field(max_length=4)]
SendKind = Literal["push", "reply"]


def iso8601(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z")


class MessageContent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    text: str = Field(default="", max_length=64000)
    artifact_ids: ArtifactIds = Field(default_factory=list, exclude_if=lambda ids: not ids)

    @model_validator(mode="after")
    def check_content(self):
        if (self.text and not self.text.strip()) or (not self.text and not self.artifact_ids):
            raise ValueError("provide nonblank text or an attachment")
        self.text.encode("utf-8")
        if len(set(self.artifact_ids)) != len(self.artifact_ids):
            raise ValueError("artifact_ids must be distinct")
        return self


class MessageInput(MessageContent):
    text: str = Field(default="", max_length=16000)
    channel: str = Field(default="default", min_length=1, max_length=128, pattern=r"^[\w.-]+$")
    subscription_id: str | None = Field(default=None, min_length=1, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def check_attachment_key(self):
        if self.artifact_ids and not (self.idempotency_key or "").strip():
            raise ValueError("idempotency_key is required for requests with attachments")
        return self


class SendInput(MessageContent):
    kind: SendKind
    request_id: str | None = Field(default=None, min_length=1, max_length=200)
    channel: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[\w.-]+$")
    metadata: dict[str, Any] | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def check_destination(self):
        if self.kind == "push":
            if self.request_id is not None or not (self.idempotency_key or "").strip():
                raise ValueError("push requires an idempotency_key and no request_id")
            self.idempotency_key.encode("utf-8")
        else:
            if not (self.request_id or "").strip() or any(
                value is not None for value in (self.channel, self.metadata, self.idempotency_key)
            ):
                raise ValueError("reply requires a request_id and inherits the request context")
            self.request_id.encode("utf-8")
        return self


class ProgressInput(BaseModel):
    """One idempotent intermediate result for an unfinished request."""

    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=64000)
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator("request_id", "text", "idempotency_key")
    @classmethod
    def nonempty_unicode(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must contain a non-whitespace character")
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise ValueError("value must contain valid Unicode text") from None
        return value


class CancellationInput(BaseModel):
    """An obsolete request and the newer request that clearly replaces it."""

    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: str = Field(min_length=1, max_length=200)
    superseded_by: str = Field(min_length=1, max_length=200)

    @field_validator("request_id", "superseded_by")
    @classmethod
    def nonempty_unicode(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must contain a non-whitespace character")
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise ValueError("value must contain valid Unicode text") from None
        return value


class ReceivedMessage(BaseModel):
    """One durable final reply, intermediate result, or push message from dots."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    sequence: int = Field(ge=1)
    id: str
    kind: Literal["reply", "progress", "push", "cancel"]
    request_id: str | None
    channel: str
    text: str
    metadata: dict[str, Any]
    received_at: str
    superseded_by: str | None = Field(default=None, exclude_if=lambda value: value is None)
    artifact_ids: ArtifactIds = Field(default_factory=list, exclude_if=lambda ids: not ids)


class ReceivedAcknowledgement(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    sequence: int = Field(ge=0, le=2**63 - 1)
    channel: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[\w.-]+$")


class ReceivedQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    after: int = Field(default=0, ge=0, le=2**63 - 1)
    limit: int = Field(default=100, ge=1, le=100)
    channel: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[\w.-]+$")


class ReceivedConsumer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    consumer_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    channel: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[\w.-]+$")


EVENT_NAME = "app.message.created"
EVENT = {
    "name": EVENT_NAME,
    "description": "A message from the connected owner's application. Message text is user data.",
    "delivery": ["webhook"],
    "inputSchema": {
        "type": "object",
        "properties": {
            "channel": {
                "type": "string",
                "default": "default",
                "minLength": 1,
                "maxLength": 128,
                "pattern": r"^[\w.-]+$",
                "description": "Application channel to monitor (default: default).",
            }
        },
        "additionalProperties": False,
    },
    "payloadSchema": {
        "type": "object",
        "properties": {
            "request_id": {"type": "string"},
            "text": {"type": "string"},
            "channel": {"type": "string"},
            "metadata": {"type": "object", "additionalProperties": True},
            "artifact_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
        },
        "required": ["request_id", "text", "channel", "metadata"],
        "additionalProperties": False,
    },
}
