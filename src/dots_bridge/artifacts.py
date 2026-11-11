"""Single-object R2 upload and download grants for Bridge messages."""

import asyncio
import base64
import binascii
import os
import re
import time
from typing import Protocol
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .config import R2Settings
from .errors import BridgeError, artifact_changed, artifact_not_found
from .models import iso8601
from .store import Store

ARTIFACT_TTL_SECONDS = 600
MAX_ARTIFACT_BYTES = 20 * 1024 * 1024
OBJECT_CONTENT_TYPE = "application/octet-stream"
OBJECT_DISPOSITION = "attachment"
OBJECT_CACHE_CONTROL = "private, no-store"


class ArtifactUploadInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0, le=MAX_ARTIFACT_BYTES)
    content_md5: str = Field(min_length=24, max_length=24)
    idempotency_key: str = Field(min_length=1, max_length=128)
    content_type: str = Field(default=OBJECT_CONTENT_TYPE, max_length=255)

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        if (
            not value.strip()
            or value in {".", ".."}
            or any(char in "/\\" or ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError("expected a basename")
        value.encode("utf-8")
        return value

    @field_validator("idempotency_key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("idempotency_key must be nonblank")
        value.encode("utf-8")
        return value

    @field_validator("content_md5")
    @classmethod
    def validate_md5(cls, value: str) -> str:
        try:
            raw = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("expected a Base64-encoded MD5 digest") from None
        if len(raw) != 16 or base64.b64encode(raw).decode("ascii") != value:
            raise ValueError("expected a Base64-encoded MD5 digest")
        return value

    @field_validator("content_type")
    @classmethod
    def validate_media_type(cls, value: str) -> str:
        media_type = value.partition(";")[0].strip()
        token = r"[A-Za-z0-9!#$%&'*+.^_`|~-]+"
        if not re.fullmatch(rf"{token}/{token}", media_type) or any(
            not 32 <= ord(char) < 127 for char in value
        ):
            raise ValueError("expected a MIME media type")
        return value


def _storage_unavailable() -> BridgeError:
    return BridgeError("artifact_storage_unavailable", "Attachment storage is unavailable.", 503)


def _upload_headers(artifact: dict) -> dict[str, str]:
    return {
        "Content-Type": OBJECT_CONTENT_TYPE,
        "Content-Length": str(artifact["size_bytes"]),
        "Content-MD5": artifact["content_md5"],
        "Content-Disposition": OBJECT_DISPOSITION,
        "Cache-Control": OBJECT_CACHE_CONTROL,
        "If-None-Match": "*",
    }


class ObjectStorage(Protocol):
    def upload_url(self, artifact: dict) -> str: ...

    def download_url(self, artifact: dict) -> str: ...

    async def head(self, artifact: dict) -> dict | None: ...


class R2Storage:
    def __init__(self, settings: R2Settings):
        access_key = os.environ.get("DOTS_BRIDGE_R2_ACCESS_KEY_ID")
        secret_key = os.environ.get("DOTS_BRIDGE_R2_SECRET_ACCESS_KEY")
        if not access_key or not secret_key:
            raise ValueError("R2 configuration requires the two Bridge R2 credential variables.")
        self.bucket = settings.bucket
        self.client = boto3.session.Session().client(
            "s3",
            endpoint_url=f"https://{settings.account_id}.r2.cloudflarestorage.com",
            region_name="auto",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                connect_timeout=5,
                read_timeout=5,
                retries={"total_max_attempts": 2, "mode": "standard"},
                request_checksum_calculation="when_required",
            ),
        )

    def _sign(self, operation: str, artifact: dict, **params) -> str:
        try:
            return self.client.generate_presigned_url(
                operation,
                Params={"Bucket": self.bucket, "Key": artifact["object_key"], **params},
                ExpiresIn=ARTIFACT_TTL_SECONDS,
            )
        except (BotoCoreError, ClientError):
            raise _storage_unavailable() from None

    def upload_url(self, artifact: dict) -> str:
        return self._sign(
            "put_object",
            artifact,
            ContentType=OBJECT_CONTENT_TYPE,
            ContentLength=artifact["size_bytes"],
            ContentMD5=artifact["content_md5"],
            ContentDisposition=OBJECT_DISPOSITION,
            CacheControl=OBJECT_CACHE_CONTROL,
            IfNoneMatch="*",
        )

    def download_url(self, artifact: dict) -> str:
        return self._sign("get_object", artifact)

    async def head(self, artifact: dict) -> dict | None:
        try:
            return await asyncio.to_thread(
                self.client.head_object, Bucket=self.bucket, Key=artifact["object_key"]
            )
        except ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                return None
            raise _storage_unavailable() from None
        except BotoCoreError:
            raise _storage_unavailable() from None


class ArtifactService:
    def __init__(self, store: Store, owner: str, storage: ObjectStorage):
        self.store = store
        self.owner = owner
        self.storage = storage

    async def request_upload(self, payload: dict) -> dict:
        try:
            upload = ArtifactUploadInput.model_validate(payload)
        except ValidationError:
            raise BridgeError(
                "invalid_artifact", "Invalid attachment upload parameters.", 422
            ) from None
        artifact_id = "art_" + uuid4().hex
        artifact = await self.store.reserve_artifact(
            self.owner, artifact_id, "artifacts/" + artifact_id, upload.model_dump()
        )
        if artifact["etag"] is not None:
            return {"artifact_id": artifact["id"], "status": "ready"}
        expires_at = iso8601(int(time.time()) + ARTIFACT_TTL_SECONDS)
        return {
            "artifact_id": artifact["id"],
            "status": "pending",
            "upload_url": self.storage.upload_url(artifact),
            "required_headers": _upload_headers(artifact),
            "expires_at": expires_at,
        }

    async def _verify_object(self, artifact: dict, *, downloading: bool) -> str:
        head = await self.storage.head(artifact)
        if head is None:
            if downloading:
                raise artifact_not_found()
            raise BridgeError("artifact_not_uploaded", "The attachment object is missing.", 409)
        etag = head.get("ETag")
        if (
            not isinstance(etag, str)
            or not etag
            or head.get("ContentLength") != artifact["size_bytes"]
            or head.get("ContentType") != OBJECT_CONTENT_TYPE
            or head.get("ContentDisposition") != OBJECT_DISPOSITION
            or head.get("CacheControl") != OBJECT_CACHE_CONTROL
            or (artifact["etag"] is not None and artifact["etag"] != etag)
        ):
            raise artifact_changed()
        return etag

    async def verify_for_send(self, ids: list[str]) -> dict[str, str]:
        artifacts = await self.store.artifacts(self.owner, ids)
        return {
            artifact["id"]: await self._verify_object(artifact, downloading=False)
            for artifact in artifacts
        }

    async def get_download(self, artifact_id: str) -> dict:
        (artifact,) = await self.store.artifacts(self.owner, [artifact_id])
        if artifact["etag"] is None:
            raise BridgeError("artifact_not_ready", "The attachment has not been published.", 409)
        await self._verify_object(artifact, downloading=True)
        expires_at = iso8601(int(time.time()) + ARTIFACT_TTL_SECONDS)
        return {
            "artifact_id": artifact_id,
            "filename": artifact["filename"],
            "content_type": artifact["content_type"],
            "size_bytes": artifact["size_bytes"],
            "download_url": self.storage.download_url(artifact),
            "expires_at": expires_at,
        }
