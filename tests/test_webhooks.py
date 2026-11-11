import asyncio
import base64
import gzip
import hashlib
import hmac
import json
import socket
from unittest.mock import AsyncMock

import httpx
import pytest

from dots_bridge.webhooks import (
    MAX_BODY_BYTES,
    MAX_RESPONSE_BYTES,
    post_webhook,
    signed_body,
    signing_key,
    validate_url,
)


def secret(key: bytes = b"a" * 32) -> str:
    return "whsec_" + base64.b64encode(key).decode("ascii")


def test_signature_covers_exact_utf8_bytes_and_headers(monkeypatch):
    monkeypatch.setattr("dots_bridge.webhooks.time.time", lambda: 1720000000.9)
    body, headers = signed_body("sub_test", secret(), "evt_same", {"text": "こんにちは"})

    assert body == '{"text":"こんにちは"}'.encode()
    assert headers["webhook-timestamp"] == "1720000000"
    assert headers["webhook-id"] == "evt_same"
    assert headers["X-MCP-Subscription-Id"] == "sub_test"
    expected = base64.b64encode(
        hmac.new(b"a" * 32, b"evt_same.1720000000." + body, hashlib.sha256).digest()
    ).decode("ascii")
    assert headers["webhook-signature"] == "v1," + expected


def test_rotation_signs_with_both_keys(monkeypatch):
    monkeypatch.setattr("dots_bridge.webhooks.time.time", lambda: 1720000000)
    body, headers = signed_body(
        "sub_test", secret(b"b" * 32), "evt_same", {"ok": True}, old_secret=secret()
    )
    actual = headers["webhook-signature"].split()
    expected = [
        "v1,"
        + base64.b64encode(
            hmac.new(key, b"evt_same.1720000000." + body, hashlib.sha256).digest()
        ).decode("ascii")
        for key in (b"b" * 32, b"a" * 32)
    ]
    assert actual == expected


@pytest.mark.parametrize(
    "value", [None, "plain", "whsec_???", secret(b"a" * 23), secret(b"a" * 65)]
)
def test_signing_key_rejects_invalid_secrets(value):
    with pytest.raises(ValueError):
        signing_key(value)


def test_signed_body_requires_a_primary_secret():
    with pytest.raises(ValueError):
        signed_body("sub_test", None, "evt_test", {})


def test_payload_size_counts_utf8_bytes():
    with pytest.raises(ValueError, match="256 KiB"):
        signed_body("sub_test", secret(), "evt_test", {"text": "あ" * 90000})


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/callback",
        "https://example.com:444/callback",
        "https://user:password@example.com/callback",
        "https://example.com/callback#fragment",
        "https://example.com/callback#",
        "https:///callback",
        "https://example.com:invalid/callback",
        "https://[::1/callback",
        "https://exam\nple.com/callback",
        "https://example.com\\@127.0.0.1/callback",
    ],
)
def test_invalid_urls_rejected(url):
    with pytest.raises(ValueError):
        validate_url(url)


def test_https_callback_with_query_is_valid():
    parsed = validate_url("https://example.com:443/callback?tenant=one")
    assert parsed.hostname == "example.com"


def mock_dns(monkeypatch, ips):
    addresses = [
        (
            socket.AF_INET6 if ":" in ip else socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            (ip, 443, 0, 0) if ":" in ip else (ip, 443),
        )
        for ip in ips
    ]
    lookup = AsyncMock(return_value=addresses)
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", lookup)
    return lookup


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ips",
    [
        [],
        ["127.0.0.1"],
        ["10.0.0.1"],
        ["169.254.169.254"],
        ["::1"],
        ["fc00::1"],
        ["::ffff:127.0.0.1"],
        ["224.0.0.1"],
        ["93.184.216.34", "192.168.1.1"],
    ],
)
async def test_private_empty_and_mixed_dns_results_are_rejected_before_connect(monkeypatch, ips):
    mock_dns(monkeypatch, ips)

    def unexpected_client(**kwargs):
        pytest.fail("No network client may be created for a rejected address")

    monkeypatch.setattr("dots_bridge.webhooks.httpx.AsyncClient", unexpected_client)
    with pytest.raises(ValueError, match="public addresses"):
        await post_webhook("https://example.com/callback", b"{}", {})


def mock_transport(monkeypatch, handler):
    constructor = httpx.AsyncClient
    options = []

    def client(**kwargs):
        options.append(kwargs)
        return constructor(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("dots_bridge.webhooks.httpx.AsyncClient", client)
    return options


@pytest.mark.asyncio
async def test_delivery_pins_validated_ip_and_preserves_host_tls_and_body(monkeypatch):
    lookup = mock_dns(monkeypatch, ["93.184.216.34", "93.184.216.35"])
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(202, json={"accepted": True}, headers={"Retry-After": "5"})

    options = mock_transport(monkeypatch, handler)
    payload = json.dumps({"text": "こんにちは"}, ensure_ascii=False).encode()
    response = await post_webhook(
        "https://example.com/callback?tenant=one", payload, {"webhook-id": "evt_same"}
    )
    assert response.status_code == 202
    assert response.json() == {"accepted": True}
    assert response.headers["Retry-After"] == "5"
    lookup.assert_awaited_once_with("example.com", 443, type=socket.SOCK_STREAM)
    assert options == [{"timeout": 10, "follow_redirects": False, "trust_env": False}]
    assert len(requests) == 1
    request = requests[0]
    assert request.url == "https://93.184.216.34/callback?tenant=one"
    assert request.headers["Host"] == "example.com"
    assert request.extensions["sni_hostname"] == "example.com"
    assert request.headers["webhook-id"] == "evt_same"
    assert request.content == payload


@pytest.mark.asyncio
@pytest.mark.parametrize("ipv6", ["2606:4700::1111", "::1"])
async def test_proxy_uses_encrypted_dns_and_rejects_mixed_addresses(monkeypatch, ipv6):
    requests = []
    proxy = "socks5://127.0.0.1:1080"
    constructor = httpx.AsyncClient

    def handle(request):
        requests.append(request)
        if request.url.host == "dns.google":
            assert request.url.params["name"] == "example.com"
            is_ipv4 = request.url.params["type"] == "A"
            record = {"type": 1 if is_ipv4 else 28, "data": "93.184.216.34" if is_ipv4 else ipv6}
            return httpx.Response(200, json={"Status": 0, "Answer": [record]})
        return httpx.Response(202)

    def client(**kwargs):
        assert kwargs.pop("proxy") == proxy
        return constructor(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr("dots_bridge.webhooks.httpx.AsyncClient", client)
    if ipv6 == "::1":
        with pytest.raises(ValueError, match="public addresses"):
            await post_webhook("https://example.com/callback", b"{}", {}, proxy_url=proxy)
        assert len(requests) == 2
    else:
        response = await post_webhook("https://example.com/callback", b"{}", {}, proxy_url=proxy)
        assert response.status_code == 202
        assert requests[-1].url == "https://93.184.216.34/callback"
        assert requests[-1].headers["Host"] == "example.com"
        assert requests[-1].extensions["sni_hostname"] == "example.com"


@pytest.mark.asyncio
async def test_redirect_is_returned_without_following_it(monkeypatch):
    mock_dns(monkeypatch, ["93.184.216.34"])
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(307, headers={"Location": "http://127.0.0.1/private"})

    mock_transport(monkeypatch, handler)
    response = await post_webhook("https://example.com/callback", b"{}", {})
    assert response.status_code == 307
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_response_limit_applies_after_decompression(monkeypatch):
    mock_dns(monkeypatch, ["93.184.216.34"])
    compressed = gzip.compress(b"x" * (MAX_RESPONSE_BYTES + 1))
    mock_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, content=compressed, headers={"Content-Encoding": "gzip"}
        ),
    )
    with pytest.raises(ValueError, match="16 KiB"):
        await post_webhook("https://example.com/callback", b"{}", {})


@pytest.mark.asyncio
async def test_small_compressed_response_is_not_decoded_twice(monkeypatch):
    mock_dns(monkeypatch, ["93.184.216.34"])
    compressed = gzip.compress(b'{"ok":true}')
    mock_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, content=compressed, headers={"Content-Encoding": "gzip"}
        ),
    )
    response = await post_webhook("https://example.com/callback", b"{}", {})
    assert response.json() == {"ok": True}
    assert "content-encoding" not in response.headers


@pytest.mark.asyncio
async def test_raw_outgoing_body_limit_is_enforced_before_dns(monkeypatch):
    lookup = mock_dns(monkeypatch, ["93.184.216.34"])
    with pytest.raises(ValueError, match="256 KiB"):
        await post_webhook("https://example.com/callback", b"x" * (MAX_BODY_BYTES + 1), {})
    lookup.assert_not_awaited()
