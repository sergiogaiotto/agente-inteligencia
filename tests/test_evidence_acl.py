"""Evidence ACL (64.0.0) — filtro '_acl_filter' do Retriever.

Trava: (a) no-op quando a flag evidence_acl_enabled está OFF (zero regressão);
(b) no-op sem clearance; (c) filtra por clearance via opa_policies.evidence_allows;
(d) fail-closed (não devolve) quando a decisão do OPA lança.
"""
from __future__ import annotations

import types

import pytest

import app.evidence.runtime as R
from app.evidence.runtime import EvidenceResult, Retriever


def _ev(cid, conf):
    return EvidenceResult(evidence_id=cid, snippet_text="x", relevance_score=1.0,
                          source_name="s", source_id="sid", confidentiality=conf)


def _settings(flag):
    return lambda: types.SimpleNamespace(evidence_acl_enabled=flag)


class TestAclFilter:
    @pytest.mark.asyncio
    async def test_flag_off_no_op(self, monkeypatch):
        monkeypatch.setattr(R, "get_settings", _settings(False))
        evs = [_ev("1", "restricted"), _ev("2", "public")]
        assert await Retriever()._acl_filter(evs, "public") == evs  # OFF → sem filtro

    @pytest.mark.asyncio
    async def test_sem_clearance_no_op(self, monkeypatch):
        monkeypatch.setattr(R, "get_settings", _settings(True))
        evs = [_ev("1", "restricted")]
        assert await Retriever()._acl_filter(evs, None) == evs  # sem clearance → sem filtro

    @pytest.mark.asyncio
    async def test_filtra_por_clearance(self, monkeypatch):
        monkeypatch.setattr(R, "get_settings", _settings(True))
        import app.core.opa_policies as P

        async def _allows(clearance, conf):
            return conf in ("public", "internal")  # simula clearance 'internal'
        monkeypatch.setattr(P, "evidence_allows", _allows)
        evs = [_ev("1", "restricted"), _ev("2", "public"), _ev("3", "confidential"), _ev("4", "internal")]
        out = await Retriever()._acl_filter(evs, "internal")
        assert [e.evidence_id for e in out] == ["2", "4"]  # esconde confidential/restricted

    @pytest.mark.asyncio
    async def test_erro_na_decisao_fail_closed(self, monkeypatch):
        monkeypatch.setattr(R, "get_settings", _settings(True))
        import app.core.opa_policies as P

        async def _boom(clearance, conf):
            raise RuntimeError("opa blip")
        monkeypatch.setattr(P, "evidence_allows", _boom)
        evs = [_ev("1", "internal")]
        # erro na avaliação → NÃO devolve a evidência (fail-closed), sem derrubar
        assert await Retriever()._acl_filter(evs, "internal") == []

    @pytest.mark.asyncio
    async def test_lista_vazia_no_op(self, monkeypatch):
        monkeypatch.setattr(R, "get_settings", _settings(True))
        assert await Retriever()._acl_filter([], "public") == []
