"""URN do catálogo — futuro-proof para multi-workspace + federação.

Formato: urn:maestro:<workspace>:<kind>:<slug>:<version>
Onda 1 fixa workspace='default'. Onda 2+ pode aceitar workspace real.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional, TypedDict

DEFAULT_WORKSPACE = "default"

# Schema NID conforme RFC 8141 simplificado: aceita a-z 0-9 - apenas (lowercase).
_SLUG_RE = re.compile(r"[^a-z0-9-]+")
_URN_RE = re.compile(
    r"^urn:maestro:([a-z0-9-]+):([a-z_]+):([a-z0-9-]+):([0-9]+\.[0-9]+\.[0-9]+)$"
)

VALID_KINDS = frozenset({"agent", "skill", "application", "recipe", "external_platform", "pipeline"})


class ParsedUrn(TypedDict):
    workspace: str
    kind: str
    slug: str
    version: str


def slugify(name: str) -> str:
    """Normaliza nome humano em slug seguro.

    'Análise Fiscal v2' → 'analise-fiscal-v2'
    'Maestro Órbita'    → 'maestro-orbita'  (acentos TRANSLITERADOS, não removidos)

    Translitera via unicodedata NFKD + remoção de combining marks — mesmo padrão
    de app/data_tables/queries.py e app/agents/engine._no_accents, sem dep extra.
    Antes desta correção, 'Órbita' virava 'rbita' (o char acentuado caía no
    _SLUG_RE e virava '-', perdendo a letra base em vez de transliterá-la).
    """
    if not name:
        return ""
    # Translitera acentos preservando a letra base: 'ó'→'o', 'ç'→'c', 'ã'→'a'.
    nfkd = unicodedata.normalize("NFKD", name)
    s = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    s = s.lower().strip()
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


def is_local_urn(urn: str, local_workspace: str = DEFAULT_WORKSPACE) -> bool:
    """True se o URN pertence ao workspace DESTA instância (PR8a — federação).

    URN malformado não é local nem remoto (ambos os helpers devolvem False)."""
    p = parse_urn(urn)
    return p is not None and p["workspace"] == local_workspace


def is_remote_urn(urn: str, local_workspace: str = DEFAULT_WORKSPACE) -> bool:
    """True se o URN pertence a OUTRO workspace (capability federada)."""
    p = parse_urn(urn)
    return p is not None and p["workspace"] != local_workspace
