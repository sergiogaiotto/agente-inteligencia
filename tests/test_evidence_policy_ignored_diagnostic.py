"""Diagnóstico honesto quando ## Evidence Policy declara fontes mas o RAG é pulado.

Footgun coberto: um Especialista em pipeline com ``## Evidence Policy`` populada
(``sources: [...]``) porém com ``require_evidence`` DESLIGADO (ou profile ``fast``)
tem as fontes silenciosamente ignoradas — respondendo "sem evidência" apesar de
KBs corretas. O diagnóstico antigo dava o conselho genérico "registre bases", que
NÃO corresponde à causa real. ``_no_evidence_diagnostic`` passa a apontar a causa
(require_evidence off) e a ação corretiva quando o chamador sinaliza fontes
ignoradas.
"""
from app.agents.engine import _no_evidence_diagnostic


def test_generic_hint_when_no_sources_declared():
    d = _no_evidence_diagnostic(sources_ignored=None)
    assert d["level"] == "info"
    assert "Registre bases" in d["text"]


def test_generic_hint_when_empty_sources_list():
    # Lista vazia é falsy → mesmo caminho do legado (sem fontes declaradas úteis).
    d = _no_evidence_diagnostic(sources_ignored=[])
    assert d["level"] == "info"
    assert "Registre bases" in d["text"]


def test_actionable_warning_when_sources_ignored():
    d = _no_evidence_diagnostic(sources_ignored=["ks-1", "ks-2"])
    assert d["level"] == "warning"
    # cita a quantidade de fontes, a seção e a causa/correção reais
    assert "2 fonte" in d["text"]
    assert "## Evidence Policy" in d["text"]
    assert "require_evidence" in d["text"]
    assert "Exigir evidência" in d["text"]


def test_singular_count_message_is_well_formed():
    d = _no_evidence_diagnostic(sources_ignored=["so-uma"])
    assert d["level"] == "warning"
    assert "1 fonte" in d["text"]
