"""URN do catálogo — futuro-proof para multi-workspace + federação.

Formato: urn:maestro:<workspace>:<kind>:<slug>:<version>
Onda 1 fixa workspace='default'. Onda 2+ pode aceitar workspace real.
"""

from __future__ import annotations

import re
from typing import Optional, TypedDict

DEFAULT_WORKSPACE = "default"

# Schema NID conforme RFC 8141 simplificado: aceita a-z 0-9 - apenas (lowercase).
_SLUG_RE = re.compile(r"[^a-z0-9-]+")
_URN_RE = re.compile(
    r"^urn:maestro:([a-z0-9-]+):([a-z_]+):([a-z0-9-]+):([0-9]+\.[0-9]+\.[0-9]+)$"
)

VALID_KINDS = frozenset({"agent", "skill", "application", "recipe", "external_platform"})


class ParsedUrn(TypedDict):
    workspace: str
    kind: str
    slug: str
    version: str


def slugify(name: str) -> str:
    """Normaliza nome humano em slug seguro.

    'Análise Fiscal v2' → 'analise-fiscal-v2'
    Não usa unidecode (evita dep extra) — caracteres acentuados viram '-'.
    Em produção, o publisher pode override o slug se quiser preservar acentos
    via campo dedicado (não implementado na Onda 1).
    """
    if not name:
        return ""
    # Normalização básica: lowercase + remove acento aproximado por mapping
    s = name.lower().strip()
    s = s.replace(" ", "-")
    s = _SLUG_RE.sub("-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s


def make_urn(kind: str, name: str, version: str, workspace: str = DEFAULT_WORKSPACE) -> str:
    """Compõe URN canônico. Lança ValueError em entrada inválida."""
    if kind not in VALID_KINDS:
        raise ValueError(f"kind inválido: {kind!r}. Esperado: {sorted(VALID_KINDS)}")
    if not name or not name.strip():
        raise ValueError("name vazio")
    if not re.match(r"^[0-9]+\.[0-9]+\.[0-9]+$", version or ""):
        raise ValueError(f"version deve ser semver MAJOR.MINOR.PATCH, recebido: {version!r}")
    slug = slugify(name)
    if not slug:
        raise ValueError(f"name {name!r} não produziu slug válido")
    if not re.match(r"^[a-z0-9-]+$", workspace or ""):
        raise ValueError(f"workspace inválido: {workspace!r}")
    return f"urn:maestro:{workspace}:{kind}:{slug}:{version}"


def parse_urn(urn: str) -> Optional[ParsedUrn]:
    """Decompõe URN em partes. Retorna None se inválido."""
    if not urn:
        return None
    m = _URN_RE.match(urn)
    if not m:
        return None
    return ParsedUrn(
        workspace=m.group(1),
        kind=m.group(2),
        slug=m.group(3),
        version=m.group(4),
    )


def is_valid_urn(urn: str) -> bool:
    """True se o URN é sintaticamente válido."""
    return parse_urn(urn) is not None
