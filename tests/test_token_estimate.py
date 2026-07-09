"""F7 — a estimativa de tokens da modal de ingestão RAG (evidence.html) usava
`chars / 4` (heurística de inglês), subestimando ~33% a contagem real do
tokenizer em texto pt-BR (tiktoken cl100k ~2.7 chars/token).

Guarda o divisor calibrado contra a medição REAL observada no teste E2E "Órbita":
2225 caracteres de markdown pt-BR → 831 tokens reais (ingestão).
"""

from __future__ import annotations

import re
from pathlib import Path

_EVIDENCE = (
    Path(__file__).resolve().parent.parent
    / "app" / "templates" / "pages" / "evidence.html"
)

# Amostra real do E2E: KB "Órbita · Base de Produtos".
_SAMPLE_CHARS = 2225
_SAMPLE_REAL_TOKENS = 831


def _ingest_divisor() -> float:
    html = _EVIDENCE.read_text(encoding="utf-8")
    m = re.search(r"ingestForm\.text\.length\s*/\s*([0-9.]+)", html)
    assert m, "heurística de tokens da ingestão não encontrada em evidence.html"
    return float(m.group(1))


def test_ingest_estimate_calibrated_for_ptbr():
    div = _ingest_divisor()
    assert 2.3 <= div <= 3.0, (
        f"divisor {div}: use ~2.7 (pt-BR / cl100k), não ~4 (inglês)."
    )


def test_ingest_estimate_within_20pct_of_real():
    import math

    div = _ingest_divisor()
    est = math.ceil(_SAMPLE_CHARS / div)
    drift = abs(est - _SAMPLE_REAL_TOKENS) / _SAMPLE_REAL_TOKENS
    assert drift <= 0.20, (
        f"estimativa {est} vs real {_SAMPLE_REAL_TOKENS} tokens diverge {drift:.0%} (> 20%)"
    )
