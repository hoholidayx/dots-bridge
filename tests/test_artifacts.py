import asyncio
import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from test_app import API_KEY, BASE, login, rpc, running_app, subscribe

from dots_bridge import AsyncBridgeClient, BridgeClient, BridgeError, DotsBridge
from dots_bridge.artifacts import (
    ARTIFACT_TTL_SECONDS,
    OBJECT_CACHE_CONTROL,
    OBJECT_CONTENT_TYPE,
    OBJECT_DISPOSITION,
    R2Storage,
)
from dots_bridge.config import R2Settings


def checksum(data):
    return base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode("ascii")


def upload_fields(key, filename, data, content_type=OBJECT_CONTENT_TYPE):
    return {
        "filename": filename,
        "size_bytes": len(data),
        "content_md5": checksum(data),
        "idempotency_key": key,
        "content_type": content_type,
    }


class LocalObjects:
    def __init__(self):
        self.objects = {}
        self.head_calls = 0
        self.unavailable = False

    def upload_url(self, artifact):
        return "https://r2.example/" + artifact["object_key"]

    download_url = upload_url

    async def head(self, artifact):
        self.head_calls += 1
        if self.unavailable:
            raise BridgeError("artifact_storage_unavailable", "Storage unavailable.", 503)
        stored = self.objects.get(artifact["object_key"])
        return stored[1] if stored is not None else None

    def put(self, grant, data):
        headers = grant["required_headers"]
        assert headers["Content-Length"] == str(len(data))
        assert headers["Content-MD5"] == checksum(data)
        assert headers["If-None-Match"] == "*"
        key = urlsplit(grant["upload_url"]).path.lstrip("/")
        if key in self.objects:
            return 412
        self.objects[key] = (
            data,
            {
                "ContentLength": len(data),
                "ContentType": headers["Content-Type"],
                "ContentDisposition": headers["Content-Disposition"],
                "CacheControl": headers["Cache-Control"],
                "ETag": hashlib.sha256(data).hexdigest(),
            },
        )
        return 200

    def get(self, grant):
        key = urlsplit(grant["download_url"]).path.lstrip("/")
        return self.objects[key][0]


async def call_tool(client, token, name, **arguments):
    response = await rpc(client, token, "tools/call", {"name": name, "arguments": arguments})
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert not result.get("isError"), result
    return result["structuredContent"]


def test_r2_upload_presigns_all_required_headers_including_empty_file(monkeypatch):
    monkeypatch.setenv("DOTS_BRIDGE_R2_ACCESS_KEY_ID", "test-access-key")
    monkeypatch.setenv("DOTS_BRIDGE_R2_SECRET_ACCESS_KEY", "test-secret-key")
    storage = R2Storage(R2Settings(account_id="a" * 32, bucket="private-bridge"))
    artifact = {"object_key": "artifacts/test", "size_bytes": 0, "content_md5": checksum(b"")}
    url = urlsplit(storage.upload_url(artifact))
    query = parse_qs(url.query)
    assert url.netloc == "a" * 32 + ".r2.cloudflarestorage.com"
    assert url.path == "/private-bridge/artifacts/test"
    assert query["X-Amz-Expires"] == [str(ARTIFACT_TTL_SECONDS)]
    assert set(query["X-Amz-SignedHeaders"][0].split(";")) == {
        "host",
        "content-type",
        "content-length",
        "content-md5",
        "content-disposition",
        "cache-control",
        "if-none-match",
    }
    download = parse_qs(urlsplit(storage.download_url(artifact)).query)
    assert download["X-Amz-SignedHeaders"] == ["host"]


async def test_two_way_attachments_over_private_http_and_mcp(tmp_path):
    objects = LocalObjects()
    async with running_app(tmp_path, artifact_storage=objects) as (http, app, bridge, webhook):
        token = (await login(http))["access_token"]
        archive = b"PK\x03\x04\x00\xff"
        dot_grant = await call_tool(
            http,
            token,
            "request_artifact_upload",
            **upload_fields("dot-upload", "报告.zip", archive, "application/zip"),
        )
        assert objects.put(dot_grant, archive) == 200
        assert objects.put(dot_grant, archive) == 412
        push = await call_tool(
            http,
            token,
            "send_to_app",
            kind="push",
            idempotency_key="push-file",
            artifact_ids=[dot_grant["artifact_id"]],
        )
        assert (push["kind"], push["request_id"], push["text"]) == ("push", None, "")
        assert await bridge.list_subscriptions() == []

        denied = await http.get(f"/api/artifacts/{dot_grant['artifact_id']}/download")
        assert denied.status_code == 401
        async with AsyncBridgeClient(
            BASE, api_key=API_KEY, transport=httpx.ASGITransport(app=app)
        ) as client:
            (receipt,) = await client.list_received()
            assert receipt.artifact_ids == [dot_grant["artifact_id"]]
            grant = await client.get_artifact_download(receipt.artifact_ids[0])
            assert grant["filename"] == "报告.zip"
            assert grant["content_type"] == "application/zip"
            assert objects.get(grant) == archive

            await subscribe(http, token)
            empty = await client.request_artifact_upload(
                **upload_fields("app-upload", "empty", b"")
            )
            assert empty["required_headers"]["Content-Disposition"] == OBJECT_DISPOSITION
            assert empty["required_headers"]["Cache-Control"] == OBJECT_CACHE_CONTROL
            assert objects.put(empty, b"") == 200
            request = await client.send_message(
                channel="chat",
                artifact_ids=[empty["artifact_id"]],
                idempotency_key="app-request",
                metadata={"source": "Aion"},
            )
            assert await bridge.process_next()
            assert webhook.events[-1][0]["data"]["artifact_ids"] == [empty["artifact_id"]]
            incoming = await call_tool(
                http, token, "get_artifact_download", artifact_id=empty["artifact_id"]
            )
            assert objects.get(incoming) == b""
            reply = await call_tool(
                http,
                token,
                "send_to_app",
                kind="reply",
                request_id=request["request_id"],
                artifact_ids=[dot_grant["artifact_id"]],
            )
            assert reply["channel"] == "chat"
            assert reply["metadata"] == {"source": "Aion"}
            state = await client.wait_for_reply(request["request_id"])
            assert state["reply"] == ""
            assert state["reply_artifact_ids"] == [dot_grant["artifact_id"]]
            (result,) = await client.list_response_messages(request["request_id"])
            assert result.artifact_ids == [dot_grant["artifact_id"]]
            await client.acknowledge_received("files", result.sequence, channel="chat")
            assert await client.get_consumer_cursor("files", channel="chat") == result.sequence


async def test_upload_retries_and_missing_or_replaced_objects(tmp_path, monkeypatch):
    objects = LocalObjects()
    async with running_app(tmp_path, artifact_storage=objects) as (_, _, bridge, _):
        payload = upload_fields("file-1", "unknown", b"some bytes")
        first = await bridge.request_artifact_upload(payload)
        artifact_id = first["artifact_id"]
        with monkeypatch.context() as clock:
            clock.setattr("dots_bridge.artifacts.time.time", lambda: 3_000_000_000)
            next_grant = await bridge.request_artifact_upload(payload)
        assert next_grant["artifact_id"] == artifact_id
        assert next_grant["expires_at"] > first["expires_at"]
        with pytest.raises(BridgeError, match="different input"):
            await bridge.request_artifact_upload({**payload, "filename": "changed"})
        send = {"kind": "push", "idempotency_key": "push-1", "artifact_ids": [artifact_id]}
        with pytest.raises(BridgeError, match="missing"):
            await bridge.send_to_app(**send)
        assert await bridge.list_received() == []

        assert objects.put(first, b"some bytes") == 200
        results = await asyncio.gather(*(bridge.send_to_app(**send) for _ in range(4)))
        assert results == [results[0]] * 4
        assert await bridge.list_received() == results[:1]
        assert await bridge.request_artifact_upload(payload) == {
            "artifact_id": artifact_id,
            "status": "ready",
        }
        with pytest.raises(BridgeError, match="different message"):
            await bridge.send_to_app("changed", **send)

        key = urlsplit(first["upload_url"]).path.lstrip("/")
        objects.objects[key][1]["ETag"] = "replacement"
        with pytest.raises(BridgeError) as changed:
            await bridge.get_artifact_download(artifact_id)
        assert changed.value.code == "artifact_changed"
        del objects.objects[key]
        with pytest.raises(BridgeError) as deleted:
            await bridge.get_artifact_download(artifact_id)
        assert deleted.value.status_code == 404
        assert await bridge.send_to_app(**send) == results[0]
        with pytest.raises(BridgeError, match="missing"):
            await bridge.send_to_app(**{**send, "idempotency_key": "push-2"})


async def test_bad_attachment_leaves_request_open_and_is_private(tmp_path, monkeypatch):
    objects = LocalObjects()
    async with running_app(tmp_path, artifact_storage=objects) as (http, _, bridge, _):
        token = (await login(http))["access_token"]
        await subscribe(http, token)
        request = await bridge.send_message("reply with a file", channel="chat")
        first = await bridge.request_artifact_upload(upload_fields("one", "file", b"ok"))
        second = await bridge.request_artifact_upload(upload_fields("two", "file", b"bad"))
        ids = [first["artifact_id"], second["artifact_id"]]
        assert objects.put(first, b"ok") == 200
        assert objects.put(second, b"bad") == 200
        key = urlsplit(second["upload_url"]).path.lstrip("/")
        objects.objects[key][1]["ContentLength"] = 200
        reply = {"kind": "reply", "request_id": request["request_id"], "artifact_ids": ids}
        with pytest.raises(BridgeError) as changed:
            await bridge.send_to_app(**reply)
        assert changed.value.code == "artifact_changed"
        assert (await bridge.get_message(request["request_id"]))["reply_status"] == "pending"
        assert await bridge.list_received() == []
        with pytest.raises(BridgeError, match="has not been published"):
            await bridge.get_artifact_download(ids[0])

        other = DotsBridge(
            bridge.settings.model_copy(update={"owner_user_id": "other"}), artifact_storage=objects
        )
        calls = objects.head_calls
        with pytest.raises(BridgeError) as forbidden:
            await other.get_artifact_download(ids[0])
        assert forbidden.value.status_code == 404
        assert objects.head_calls == calls

        objects.objects[key][1]["ContentLength"] = 3
        objects.unavailable = True
        with pytest.raises(BridgeError) as unavailable:
            await bridge.send_to_app(**reply)
        assert unavailable.value.status_code == 503
        assert (await bridge.get_message(request["request_id"]))["reply_status"] == "pending"

        objects.unavailable = False

        def fail_receipt(db, row):
            raise RuntimeError("receipt insertion failed")

        with monkeypatch.context() as faults:
            faults.setattr(bridge.store, "_append_reply", fail_receipt)
            with pytest.raises(RuntimeError, match="receipt insertion failed"):
                await bridge.send_to_app(**reply)
        assert (await bridge.get_message(request["request_id"]))["reply_status"] == "pending"
        with pytest.raises(BridgeError, match="has not been published"):
            await bridge.get_artifact_download(ids[0])
        saved = await bridge.send_to_app(**reply)
        assert saved["artifact_ids"] == ids
        with pytest.raises(BridgeError, match="different final reply"):
            await bridge.send_to_app(**{**reply, "artifact_ids": ids[::-1]})


def test_sync_client_passes_only_attachment_references_to_bridge():
    fields = upload_fields("upload", "opaque", b"\x00")
    artifact_id = "art_" + "0" * 32

    def handle(request):
        assert request.headers["authorization"] == "Bearer " + API_KEY
        if request.url.path == "/api/artifacts/uploads":
            return httpx.Response(200, json={"artifact_id": artifact_id, "status": "pending"})
        if request.url.path == "/api/messages":
            assert b'"artifact_ids":["' + artifact_id.encode() + b'"]' in request.content
            return httpx.Response(202, json={"request_id": "req_1"})
        assert request.url.path == f"/api/artifacts/{artifact_id}/download"
        return httpx.Response(200, json={"artifact_id": artifact_id})

    with BridgeClient(BASE, api_key=API_KEY, transport=httpx.MockTransport(handle)) as client:
        assert client.request_artifact_upload(**fields)["artifact_id"] == artifact_id
        assert (
            client.send_message(artifact_ids=[artifact_id], idempotency_key="message")["request_id"]
            == "req_1"
        )
        assert client.get_artifact_download(artifact_id)["artifact_id"] == artifact_id


async def test_schema_five_upgrade_preserves_text_retries_and_cursor(tmp_path):
    async with running_app(tmp_path) as (http, _, bridge, _):
        token = (await login(http))["access_token"]
        await subscribe(http, token)
        sent = await bridge.send_message("original", channel="chat", idempotency_key="request")
        reply = await bridge.send_to_app("final", kind="reply", request_id=sent["request_id"])
        push = await bridge.send_to_app("note", kind="push", idempotency_key="push")
        await bridge.acknowledge_received("consumer", push["sequence"])

        def emulate_schema_five(db):
            db.execute("BEGIN IMMEDIATE")
            db.execute("ALTER TABLE messages DROP COLUMN artifact_ids")
            db.execute("ALTER TABLE messages DROP COLUMN reply_artifact_ids")
            db.execute("ALTER TABLE received_messages DROP COLUMN artifact_ids")
            db.execute("DROP TABLE artifacts")
            db.execute("UPDATE bridge_meta SET value=? WHERE key='schema'", (b"5",))

        await bridge.store.run(emulate_schema_five)
        migrated = DotsBridge(bridge.settings)
        await migrated.initialize()
        assert await migrated.list_received() == [reply, push]
        assert await migrated.get_consumer_cursor("consumer") == push["sequence"]
        repeated_request = await migrated.send_message(
            "original", channel="chat", idempotency_key="request"
        )
        assert repeated_request["request_id"] == sent["request_id"]
        assert (
            await migrated.send_to_app("final", kind="reply", request_id=sent["request_id"])
            == reply
        )
        assert await migrated.send_to_app("note", kind="push", idempotency_key="push") == push
        fresh = await migrated.send_to_app("new", kind="push", idempotency_key="new")
        assert fresh["sequence"] == push["sequence"] + 1
