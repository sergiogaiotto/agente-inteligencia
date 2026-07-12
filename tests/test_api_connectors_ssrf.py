"""SEC-01 — guarda SSRF no egress de api-connectors.

O proxy/health/test/introspect/extract-cookie fazem requests outbound a partir
de dado configurável (base_url / campos do request) e são alcançáveis por cookie
OU X-API-Key. Antes desta guarda, um principal autenticado apontava para
169.254.169.254 (metadata da cloud) ou serviços internos → SSRF autenticado.

Os IPs abaixo são LITERAIS: socket.getaddrinfo os devolve sem rede (determinístico,
hermético). Onde há hostname, `ssrf.socket.getaddrinfo` é monkeypatchado.
"""

from __future__ import annotations

import asyncio

import pytest

import app.core.ssrf as ssrf
from app.core.ssrf import SSRFError
from app.routes.api_connectors import (
    ExtractCookieRequest,
    InlineTestRequest,
    _client_kwargs,
    _guard_egress,
)
# Alias: importar como `test_inline`/`extract_cookie` faria o pytest coletar o
# handler `test_inline` como se fosse um caso de teste.
from app.routes.api_connectors import extract_cookie as _extract_cookie_handler
from app.routes.api_connectors import test_inline as _test_inline_handler


def _patch_resolve(monkeypatch, ips):
    def fake(host, port, *a, **k):
        return [(2, 1, 6, "", (ip, port)) for ip in ips]
    monkeypatch.setattr(ssrf.socket, "getaddrinfo", fake)


class TestGuardEgress:
    @pytest.mark.parametrize("url", [
        "http://169.254.169.254/latest/meta-data/",   # metadata cloud (link-local)
        "http://127.0.0.1:8000/admin",                # loopback
        "http://10.0.0.5/internal",                   # privado
        "http://192.168.1.1/",                        # privado
        "http://[::1]/",                              # loopback ipv6
        "http://172.16.0.9/",                         # privado
    ])
    def test_bloqueia_ip_interno_literal(self, url):
        with pytest.raises(SSRFError):
            _guard_egress(url)

    def test_permite_ip_publico_literal(self):
        # 93.184.216.34 (example.com) é público → não levanta.
        assert _guard_egress("http://93.184.216.34/") is None
        assert _guard_egress("https://93.184.216.34/x") is None

    def test_bloqueia_esquema_invalido(self):
        with pytest.raises(SSRFError):
            _guard_egress("file:///etc/passwd")
        with pytest.raises(SSRFError):
            _guard_egress("ftp://example.com")

    def test_hostname_privado_via_dns(self, monkeypatch):
        _patch_resolve(monkeypatch, ["10.1.2.3"])
        with pytest.raises(SSRFError):
            _guard_egress("http://interno.corp.local/x")

    def test_hostname_publico_via_dns(self, monkeypatch):
        _patch_resolve(monkeypatch, ["93.184.216.34"])
        assert _guard_egress("https://api.publica.com/v1") is None

    def test_dns_misto_bloqueia(self, monkeypatch):
        # público + privado no mesmo host → bloqueia (defesa DNS misto)
        _patch_resolve(monkeypatch, ["93.184.216.34", "10.0.0.1"])
        with pytest.raises(SSRFError):
            _guard_egress("https://rebind.example/x")


class TestClientKwargsNoRedirect:
    def test_follow_redirects_desligado(self):
        # SEC-01: redirect não pode pular a validação até um host interno.
        assert _client_kwargs({})["follow_redirects"] is False


class TestHandlersBloqueiam:
    def test_test_inline_bloqueia_metadata(self):
        res = asyncio.run(_test_inline_handler(InlineTestRequest(base_url="http://169.254.169.254")))
        assert res["ok"] is False
        assert "SSRF" in res["error"]

    def test_test_inline_bloqueia_loopback(self):
        res = asyncio.run(_test_inline_handler(InlineTestRequest(base_url="http://127.0.0.1:8000")))
        assert res["ok"] is False and "SSRF" in res["error"]

    def test_extract_cookie_bloqueia_interno(self):
        res = asyncio.run(
            _extract_cookie_handler(
                ExtractCookieRequest(login_url="http://10.0.0.5/login", login_body={})
            )
        )
        assert res["ok"] is False and "SSRF" in res["error"]
