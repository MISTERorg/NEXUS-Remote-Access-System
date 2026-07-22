"""
transport/tls_context.py
------------------------
TLS/mTLS context factory.

Used by both the relay server and agents to build SSL contexts
with the correct certificate configuration.
"""

from __future__ import annotations

import ssl
from pathlib import Path
from typing import Optional

from config.settings import settings
from utils.logger import get_logger

log = get_logger("nexus.transport.tls")


class TLSContextFactory:
    """Builds ssl.SSLContext objects for server and client use."""

    @staticmethod
    def server_context(
        cert_file: Optional[Path] = None,
        key_file: Optional[Path] = None,
        ca_file: Optional[Path] = None,
        require_client_cert: Optional[bool] = None,
    ) -> ssl.SSLContext:
        """
        Build a server-side TLS context.
        Optionally enforces mTLS by requiring client certificates.
        """
        cert = cert_file or settings.tls.cert_file
        key = key_file or settings.tls.key_file
        ca = ca_file or settings.tls.ca_file
        mtls = require_client_cert if require_client_cert is not None else settings.tls.require_client_cert

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3

        try:
            ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
        except (FileNotFoundError, ssl.SSLError) as e:
            log.warning("tls.cert_load_failed", error=str(e),
                        msg="Using unencrypted fallback — generate certs for production!")
            return ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)

        if mtls and ca and Path(ca).exists():
            ctx.verify_mode = ssl.CERT_REQUIRED
            ctx.load_verify_locations(cafile=str(ca))
            log.info("tls.server_context_created", mode="mTLS")
        else:
            ctx.verify_mode = ssl.CERT_NONE
            log.info("tls.server_context_created", mode="TLS_server_only")

        return ctx

    @staticmethod
    def client_context(
        ca_file: Optional[Path] = None,
        cert_file: Optional[Path] = None,
        key_file: Optional[Path] = None,
        verify: bool = True,
    ) -> ssl.SSLContext:
        """
        Build a client-side TLS context.
        Optionally provides a client certificate for mTLS.
        """
        ca = ca_file or settings.tls.ca_file

        if verify:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_3
            if ca and Path(ca).exists():
                ctx.load_verify_locations(cafile=str(ca))
            else:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                log.warning("tls.no_ca_file", msg="Server certificate verification disabled")
        else:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

        cert = cert_file or settings.tls.cert_file
        key = key_file or settings.tls.key_file
        if cert and key and Path(cert).exists() and Path(key).exists():
            ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
            log.info("tls.client_context_created", mode="mTLS_client")
        else:
            log.info("tls.client_context_created", mode="TLS_client_only")

        return ctx

    @staticmethod
    def insecure_client_context() -> ssl.SSLContext:
        """
        Development-only context that skips all certificate verification.
        NEVER use in production.
        """
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        log.warning("tls.insecure_context", msg="Certificate verification DISABLED — DEV ONLY")
        return ctx
