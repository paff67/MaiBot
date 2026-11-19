"""Shared TLS / mTLS configuration helper for WeChat edge HTTP clients."""

from pathlib import Path
from typing import Any, Dict, Optional
import os
import ssl


def _resolve_optional_path(explicit: Optional[str], env_key: str) -> str:
    if explicit is not None:
        return str(explicit).strip()
    return str(os.environ.get(env_key, "") or "").strip()


def build_edge_ssl_context(
    edge_url: str,
    *,
    ca_path: Optional[str] = None,
    cert_path: Optional[str] = None,
    key_path: Optional[str] = None,
) -> Optional[ssl.SSLContext]:
    """Build an ``ssl.SSLContext`` for HTTPS edge endpoints using ``MAIBOT_MTLS_*`` env vars.

    Returns ``None`` for non-HTTPS URLs or when no custom CA/client certificate is configured
    (falling back to ``httpx`` default TLS verification for standard HTTPS).
    """
    if not str(edge_url or "").strip().lower().startswith("https://"):
        return None

    resolved_ca = _resolve_optional_path(ca_path, "MAIBOT_MTLS_CA")
    resolved_cert = _resolve_optional_path(cert_path, "MAIBOT_MTLS_CERT")
    resolved_key = _resolve_optional_path(key_path, "MAIBOT_MTLS_KEY")

    if bool(resolved_cert) != bool(resolved_key):
        raise ValueError("MAIBOT_MTLS_CERT and MAIBOT_MTLS_KEY must both be configured for mTLS")

    if not resolved_ca and not resolved_cert:
        return None

    for label, path_str in (
        ("MAIBOT_MTLS_CA", resolved_ca),
        ("MAIBOT_MTLS_CERT", resolved_cert),
        ("MAIBOT_MTLS_KEY", resolved_key),
    ):
        if path_str and not Path(path_str).is_file():
            raise FileNotFoundError(f"{label} file not found: {path_str}")

    ctx = ssl.create_default_context(cafile=resolved_ca or None)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = True
    if resolved_cert and resolved_key:
        ctx.load_cert_chain(certfile=resolved_cert, keyfile=resolved_key)
    return ctx


def build_edge_httpx_kwargs(
    edge_url: str,
    *,
    token: str = "",
    timeout: float = 60.0,
    ca_path: Optional[str] = None,
    cert_path: Optional[str] = None,
    key_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Build keyword arguments for ``httpx.Client`` / ``httpx.AsyncClient`` with optional mTLS."""
    kwargs: Dict[str, Any] = {
        "timeout": timeout,
        "headers": {"Authorization": "Bearer " + token} if token else {},
    }
    ssl_ctx = build_edge_ssl_context(
        edge_url,
        ca_path=ca_path,
        cert_path=cert_path,
        key_path=key_path,
    )
    if ssl_ctx is not None:
        kwargs["verify"] = ssl_ctx
    return kwargs
