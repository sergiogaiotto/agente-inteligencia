"""Guarda SSRF para egress de federação (PR8c).

Antes de QUALQUER request outbound a um peer, valida a URL: só https (http via
flag dev), resolve o host e REJEITA se QUALQUER IP resolvido for privado/loopback/
link-local/reservado/multicast/unspecified — inclui 169.254.169.254 (metadata de
cloud, que é link-local). Combine SEMPRE com `follow_redirects=False` + timeout +
cap de tamanho de resposta no cliente httpx.

Limitação residual conhecida: DNS rebinding (host resolve p/ IP público na
validação e p/ IP interno no connect). Mitigado parcialmente validando TODOS os
endereços resolvidos + sem seguir redirects; pinning de IP no connect seria a
defesa completa (evolução futura — quebra SNI/TLS com httpx simples).
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class SSRFError(ValueError):
    """URL rejeitada pela guarda SSRF (host não-público ou esquema proibido)."""


def _ip_is_blocked(ip: str) -> bool:
    """True se o IP NÃO é roteável publicamente (deve ser bloqueado)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # não-parseável → bloqueia por segurança
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local      # cobre 169.254.0.0/16 (metadata cloud)
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def validate_public_url(url: str, *, allow_http: bool = False) -> str:
    """Valida que `url` aponta para um host PÚBLICO. Devolve a URL (trim) ou
    levanta SSRFError. Resolve TODOS os IPs do host e bloqueia se QUALQUER um for
    não-público (defesa contra registros DNS mistos)."""
    if not url or not isinstance(url, str):
        raise SSRFError("URL vazia")
    parsed = urlparse(url.strip())
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("https", "http"):
        raise SSRFError(f"esquema não permitido: {scheme!r}")
    if scheme == "http" and not allow_http:
        raise SSRFError("http não permitido (use https ou habilite federation.dev_allow_http)")
    host = parsed.hostname
    if not host:
        raise SSRFError("host ausente na URL")
    port = parsed.port or (443 if scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise SSRFError(f"host não resolve: {host}")
    ips = {info[4][0] for info in infos}
    if not ips:
        raise SSRFError(f"host sem endereços: {host}")
    for ip in ips:
        if _ip_is_blocked(ip):
            raise SSRFError(f"host resolve para IP não-público: {ip}")
    return url.strip()
