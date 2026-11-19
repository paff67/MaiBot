"""Offline unit and localhost mTLS loopback tests for WeChat edge HTTP clients."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address
from pathlib import Path
from typing import Dict, Tuple
from urllib.parse import urlsplit
import hashlib
import json
import sqlite3
import ssl
import threading
import time

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
import httpx
import pytest

from src.companion_vault.asset_pipeline import AssetPipeline
from src.companion_vault.group_members import sync_chat_directory_async
from src.companion_vault.message_media_worker import MessageMediaQueue
from src.companion_vault.truth_db import CompanionTruthDB
from src.platform_io.wechat.driver import configured_wechat_drivers
from src.platform_io.wechat.edge_client import WeChatEdgeClient
from src.platform_io.wechat.edge_tls import build_edge_httpx_kwargs, build_edge_ssl_context
from src.platform_io.wechat.message_ingestor import MessageLedger, poll


def _generate_mtls_bundle(tmp_path: Path) -> Dict[str, Path]:
    """Generate an ephemeral CA, 127.0.0.1 server cert, and client cert in tmp_path."""
    now = datetime.now(timezone.utc)

    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "MaiBot Test CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    srv_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    srv_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    srv_cert = (
        x509.CertificateBuilder()
        .subject_name(srv_name)
        .issuer_name(ca_name)
        .public_key(srv_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(IPv4Address("127.0.0.1"))]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    cli_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cli_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "maibot-dmit-client")])
    cli_cert = (
        x509.CertificateBuilder()
        .subject_name(cli_name)
        .issuer_name(ca_name)
        .public_key(cli_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    paths = {
        "ca": tmp_path / "ca.crt",
        "server_cert": tmp_path / "server.crt",
        "server_key": tmp_path / "server.key",
        "client_cert": tmp_path / "client.crt",
        "client_key": tmp_path / "client.key",
    }
    paths["ca"].write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    paths["server_cert"].write_bytes(srv_cert.public_bytes(serialization.Encoding.PEM))
    paths["server_key"].write_bytes(
        srv_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    paths["client_cert"].write_bytes(cli_cert.public_bytes(serialization.Encoding.PEM))
    paths["client_key"].write_bytes(
        cli_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return paths


SAMPLE_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
SAMPLE_PNG_SHA = hashlib.sha256(SAMPLE_PNG).hexdigest()
SAMPLE_ACCOUNT = "a" * 32


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/health":
            payload = json.dumps({"ok": True, "auth": self.headers.get("Authorization", "")}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if path == "/wechat/chat_snapshot":
            payload = json.dumps(
                {
                    "protocol_version": 2,
                    "account": "acc1",
                    "chat_id": "wxid_owner",
                    "xml": "<hierarchy/>",
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if path == "/messages/events":
            payload = json.dumps(
                {
                    "epoch": "epoch-1",
                    "max_cursor": 1,
                    "last_scan_ms": int(time.time() * 1000),
                    "events": [
                        {
                            "seq": 1,
                            "account": SAMPLE_ACCOUNT,
                            "msg_id": "101",
                            "chat_id": "wxid_owner",
                            "revision": "rev-1",
                            "observed_at_ms": 1700000000000,
                            "row": {
                                "msgId": "101",
                                "talker": "wxid_owner",
                                "isSend": "0",
                                "type": "3",
                                "imgPath": "THUMBNAIL_DIRPATH://th_101",
                                "content": "<msg><img md5='0123456789abcdef0123456789abcdef'/></msg>",
                                "createTime": "1700000000000",
                            },
                        }
                    ],
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if path == "/wechat/group_members":
            payload = json.dumps(
                {
                    "ok": True,
                    "account": SAMPLE_ACCOUNT,
                    "group_id": "123@chatroom",
                    "user_ids": ["wxid_owner", "wxid_b"],
                    "contacts": {"123@chatroom": {"nickname": "测试群", "remark": ""}},
                },
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if path == "/messages/media_resolve":
            payload = json.dumps(
                {
                    "ok": True,
                    "state": "resolved",
                    "chat_id": "wxid_owner",
                    "file": {
                        "account": SAMPLE_ACCOUNT,
                        "kind": "image",
                        "relative_path": "01/0123456789abcdef0123456789abcdef.png",
                        "mtime_ns": 1000,
                        "ctime_ns": 1000,
                        "size": len(SAMPLE_PNG),
                        "sha256": SAMPLE_PNG_SHA,
                        "mime_type": "image/png",
                        "representation": "original",
                        "checksum_verified": False,
                    },
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if path == "/wechat/media_file":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("X-Content-SHA256", SAMPLE_PNG_SHA)
            self.send_header("Content-Length", str(len(SAMPLE_PNG)))
            self.end_headers()
            self.wfile.write(SAMPLE_PNG)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return


def _start_mtls_server(bundle: Dict[str, Path]) -> Tuple[ThreadingHTTPServer, int, threading.Thread]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    srv_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    srv_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    srv_ctx.load_cert_chain(certfile=str(bundle["server_cert"]), keyfile=str(bundle["server_key"]))
    srv_ctx.load_verify_locations(cafile=str(bundle["ca"]))
    srv_ctx.verify_mode = ssl.CERT_REQUIRED
    server.socket = srv_ctx.wrap_socket(server.socket, server_side=True)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port, thread


def test_http_url_ignores_mtls_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIBOT_MTLS_CA", "/nonexistent/ca.crt")
    monkeypatch.setenv("MAIBOT_MTLS_CERT", "/nonexistent/client.crt")
    monkeypatch.setenv("MAIBOT_MTLS_KEY", "/nonexistent/client.key")

    assert build_edge_ssl_context("http://100.87.39.37:8765") is None
    kwargs = build_edge_httpx_kwargs("http://100.87.39.37:8765", token="secret", timeout=30)
    assert "verify" not in kwargs
    assert kwargs["timeout"] == 30
    assert kwargs["headers"] == {"Authorization": "Bearer secret"}


def test_https_mtls_validation_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dummy = tmp_path / "dummy.pem"
    dummy.write_text("x", encoding="utf-8")

    monkeypatch.setenv("MAIBOT_MTLS_CERT", str(dummy))
    monkeypatch.delenv("MAIBOT_MTLS_KEY", raising=False)
    monkeypatch.delenv("MAIBOT_MTLS_CA", raising=False)
    with pytest.raises(ValueError, match="MAIBOT_MTLS_CERT and MAIBOT_MTLS_KEY"):
        build_edge_ssl_context("https://127.0.0.1:8443")

    monkeypatch.delenv("MAIBOT_MTLS_CERT", raising=False)
    monkeypatch.setenv("MAIBOT_MTLS_CA", str(tmp_path / "missing-ca.crt"))
    with pytest.raises(FileNotFoundError, match="MAIBOT_MTLS_CA"):
        build_edge_ssl_context("https://127.0.0.1:8443")


@pytest.mark.asyncio
async def test_mtls_loopback_sync_and_async_edge_clients(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _generate_mtls_bundle(tmp_path)
    server, port, thread = _start_mtls_server(bundle)
    base_url = f"https://127.0.0.1:{port}"
    try:
        # 1. 未提供 client cert 且未信任自定义 CA -> 握手/校验失败
        monkeypatch.delenv("MAIBOT_MTLS_CA", raising=False)
        monkeypatch.delenv("MAIBOT_MTLS_CERT", raising=False)
        monkeypatch.delenv("MAIBOT_MTLS_KEY", raising=False)
        with httpx.Client(**build_edge_httpx_kwargs(base_url, timeout=5)) as plain_client:
            with pytest.raises(httpx.HTTPError):
                plain_client.get(base_url + "/health")

        # 2. 仅配置 CA、缺少客户端证书 -> mTLS 服务端 CERT_REQUIRED 拒绝连接
        monkeypatch.setenv("MAIBOT_MTLS_CA", str(bundle["ca"]))
        with httpx.Client(**build_edge_httpx_kwargs(base_url, timeout=5)) as ca_only_client:
            with pytest.raises(httpx.HTTPError):
                ca_only_client.get(base_url + "/health")

        # 3. 完整配置 MAIBOT_MTLS_CA / CERT / KEY -> 同步 httpx.Client 与异步 WeChatEdgeClient 均通过双向 TLS 认证
        monkeypatch.setenv("MAIBOT_MTLS_CERT", str(bundle["client_cert"]))
        monkeypatch.setenv("MAIBOT_MTLS_KEY", str(bundle["client_key"]))
        with httpx.Client(**build_edge_httpx_kwargs(base_url, token="tok-123", timeout=5)) as mtls_client:
            resp = mtls_client.get(base_url + "/health")
            assert resp.status_code == 200
            assert resp.json() == {"ok": True, "auth": "Bearer tok-123"}

        edge_client = WeChatEdgeClient(base_url, "acc1", token="tok-async")
        try:
            snap = await edge_client.snapshot("wxid_owner")
            assert snap["account"] == "acc1"
            assert snap["chat_id"] == "wxid_owner"
        finally:
            await edge_client.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_decoupled_source_and_mtls_transport_url_preserves_db_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Option A: SQLite 主键保留逻辑 source (http://100.87.39.37:8765)，真实 HTTP/mTLS 请求走 transport_url。"""
    bundle = _generate_mtls_bundle(tmp_path)
    server, port, thread = _start_mtls_server(bundle)
    logical_source = "http://100.87.39.37:8765"
    transport_url = f"https://127.0.0.1:{port}"
    monkeypatch.setenv("MAIBOT_MTLS_CA", str(bundle["ca"]))
    monkeypatch.setenv("MAIBOT_MTLS_CERT", str(bundle["client_cert"]))
    monkeypatch.setenv("MAIBOT_MTLS_KEY", str(bundle["client_key"]))

    ledger_path = tmp_path / "message_ledger.db"
    truth_path = tmp_path / "companion_truth.db"
    try:
        # 1. WeChatEdgeClient + configured_wechat_drivers: driver_id/source 保持逻辑键，HTTP 走 mTLS transport_url
        cfg_path = tmp_path / "wechat_driver.toml"
        cfg_path.write_text(
            "\n".join(
                [
                    "version = 1",
                    f'ledger_path = "{ledger_path.as_posix()}"',
                    f'truth_path = "{truth_path.as_posix()}"',
                    "[[drivers]]",
                    "enabled = true",
                    f'source = "{logical_source}"',
                    f'transport_url = "{transport_url}"',
                    'account = "acc1"',
                ]
            ),
            encoding="utf-8",
        )
        [driver] = configured_wechat_drivers(str(cfg_path))
        try:
            assert driver.edge.source == logical_source
            assert driver.edge.transport_url == transport_url
            snap = await driver.edge.snapshot("wxid_owner")
            assert snap["account"] == "acc1"
        finally:
            await driver.edge.close()

        # 若未在 TOML 写 transport_url，则由入口环境变量 MAIBOT_EDGE_TRANSPORT_URL 接管
        cfg_env_path = tmp_path / "wechat_driver_env.toml"
        cfg_env_path.write_text(
            "\n".join(
                [
                    "version = 1",
                    f'ledger_path = "{ledger_path.as_posix()}"',
                    f'truth_path = "{truth_path.as_posix()}"',
                    "[[drivers]]",
                    "enabled = true",
                    f'source = "{logical_source}"',
                    'account = "acc1"',
                ]
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("MAIBOT_EDGE_TRANSPORT_URL", transport_url)
        [env_driver] = configured_wechat_drivers(str(cfg_env_path))
        try:
            assert env_driver.edge.source == logical_source
            assert env_driver.edge.transport_url == transport_url
        finally:
            await env_driver.edge.close()

        # 2. message_ingestor.poll: 从 mTLS transport_url 拉取事件，账本与游标主键仍为 logical_source
        ledger = MessageLedger(str(ledger_path))
        try:
            with httpx.Client(**build_edge_httpx_kwargs(transport_url, timeout=5)) as sync_client:
                epoch = poll(ledger, sync_client, logical_source, "", transport_url=transport_url)
                assert epoch == "epoch-1"
                assert ledger.cursor(logical_source, "epoch-1") == 1
                assert [r[0] for r in ledger.db.execute("SELECT DISTINCT source FROM messages").fetchall()] == [
                    logical_source
                ]
                assert [r[0] for r in ledger.db.execute("SELECT source FROM source_health").fetchall()] == [
                    logical_source
                ]
        finally:
            ledger.db.close()

        # 3. MessageMediaQueue.run_one: 校验 job.source == logical_source 后走 mTLS transport_url 拉取原件
        ro_ledger = sqlite3.connect(ledger_path.resolve().as_uri() + "?mode=ro", uri=True)
        truth = CompanionTruthDB(str(truth_path))
        try:
            assets = AssetPipeline(truth, spool=str(tmp_path / "spool"))
            with httpx.Client(**build_edge_httpx_kwargs(transport_url, timeout=5)) as sync_client:
                queue = MessageMediaQueue(
                    assets,
                    ro_ledger,
                    logical_source,
                    sync_client,
                    transport_url=transport_url,
                )
                assert queue.enqueue() == 1
                assert queue.run_one() is True
                assert queue.run_one() is True
                states = {
                    row["state"]
                    for row in truth.conn.execute(
                        "SELECT state FROM message_media_jobs WHERE source=?",
                        (logical_source,),
                    ).fetchall()
                }
                assert "ready" in states
        finally:
            truth.close()
            ro_ledger.close()

        # 4. sync_chat_directory_async: 从 mTLS transport_url 拉取群成员，落库主键为 logical_source
        edge_for_dir = WeChatEdgeClient(
            logical_source,
            SAMPLE_ACCOUNT,
            transport_url=transport_url,
        )
        try:
            g_name = await sync_chat_directory_async(
                str(truth_path),
                edge_for_dir.client,
                source=logical_source,
                account=SAMPLE_ACCOUNT,
                chat_id="123@chatroom",
                transport_url=edge_for_dir.transport_url,
            )
            assert g_name == "测试群"
        finally:
            await edge_for_dir.close()

        verify_truth = CompanionTruthDB(str(truth_path))
        try:
            members = verify_truth.conn.execute(
                "SELECT user_id FROM group_members WHERE source=? AND account=? AND group_id=? ORDER BY user_id",
                (logical_source, SAMPLE_ACCOUNT, "123@chatroom"),
            ).fetchall()
            assert [r[0] for r in members] == ["wxid_b", "wxid_owner"]
        finally:
            verify_truth.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
