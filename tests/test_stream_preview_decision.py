"""Backlog 4 do arco condicional (36.2.1): linha DECISAO × streaming/UI ao vivo.

O stream é por EVENTO de step (não por token). Exposições reais (review):
(1) `pipeline_steps[].output` CRU virava balão de chat AO VIVO no workspace —
e divergia do F5, que stripa no reload → o engine agora anota
`output_display` por step (autor + eco via cadeia) e o balão o usa;
(2) `output_preview` do `agent_done` (consumidores SSE externos) — strippado
com fallback de eco; só computado quando há callback (custo).
`output` cru segue no trace/gate; a trilha do modal do fluxograma segue crua
de propósito (trilha≈trace, decisão da Fase 1).
"""
from pathlib import Path

import pytest

SKILL_MD = """# Triagem
## Decisions
```json
{ "escalar": ["sim", "não"], "severidade": ["baixa", "média", "alta"] }
```
"""


def _bind_contrato(monkeypatch, com_contrato: set):
    import app.agents.engine as eng

    async def _agent(aid):
        return {"id": aid, "skill_id": "sk-1" if aid in com_contrato else ""}

    async def _skill(_id):
        return {"id": _id, "raw_content": SKILL_MD}

    monkeypatch.setattr(eng, "_topo_agent", _agent)
    monkeypatch.setattr(eng.skills_repo, "find_by_id", _skill)
    return eng


class TestStripForDisplayMulti:
    @pytest.mark.asyncio
    async def test_autor_com_contrato(self, monkeypatch):
        eng = _bind_contrato(monkeypatch, {"ag-a"})
        got = await eng._strip_for_display_multi("Caso grave.\nDECISAO: escalar=sim", ["ag-a"])
        assert got == "Caso grave."

    @pytest.mark.asyncio
    async def test_eco_cai_no_schema_do_upstream(self, monkeypatch):
        # especialista SEM contrato ecoou a linha do router — o fallback pela
        # cadeia stripa (assimetria achada pelo review pré-push)
        eng = _bind_contrato(monkeypatch, {"ag-router"})
        got = await eng._strip_for_display_multi(
            "Resolvido conforme triagem.\nDECISAO: escalar=sim",
            ["ag-esp", "ag-router"])
        assert got == "Resolvido conforme triagem."

    @pytest.mark.asyncio
    async def test_prosa_sem_contrato_fica_e_fail_safe(self, monkeypatch):
        eng = _bind_contrato(monkeypatch, set())
        txt = "Análise.\nDecisão: aprovado o crédito"
        assert await eng._strip_for_display_multi(txt, ["ag-x", "ag-y"]) == txt

        import app.agents.engine as eng2

        async def _boom(_id):
            raise RuntimeError("db off")

        monkeypatch.setattr(eng2, "_topo_agent", _boom)
        raw = "Resposta.\nDECISAO: escalar=sim"
        assert await eng2._strip_for_display_multi(raw, ["ag-1"]) == raw


class TestDisplayPreview:
    @pytest.mark.asyncio
    async def test_limite_aplicado_apos_o_strip(self, monkeypatch):
        eng = _bind_contrato(monkeypatch, {"ag-a"})
        longa = ("x" * 400) + "\nDECISAO: escalar=sim"
        got = await eng._display_preview(longa, "ag-a")
        assert len(got) == 300 and "DECISAO" not in got

    @pytest.mark.asyncio
    async def test_fallback_de_eco_no_preview(self, monkeypatch):
        eng = _bind_contrato(monkeypatch, {"ag-router"})
        got = await eng._display_preview(
            "Eco aqui.\nDECISAO: escalar=sim", "ag-esp",
            fallback_agent_ids=["ag-router"])
        assert got == "Eco aqui."


class TestFiacaoNaEmissaoENosBaloes:
    def test_agent_done_usa_preview_condicionado_ao_callback(self):
        src = Path("app/agents/engine.py").read_text(encoding="utf-8")
        i = src.find('"type": "agent_done"')
        assert i != -1
        bloco = src[i:i + 1400]
        assert "await _display_preview(" in bloco
        assert "if progress_callback is not None else" in bloco

    def test_steps_ganham_output_display_quando_difere(self):
        src = Path("app/agents/engine.py").read_text(encoding="utf-8")
        assert '_s["output_display"] = _disp' in src
        # cru preservado por design (trace/gate) — o campo só existe se difere
        assert "if _disp != _out_s:" in src

    def test_balao_do_workspace_usa_output_display(self):
        ws = Path("app/templates/pages/workspace.html").read_text(encoding="utf-8")
        assert "step.output_display!==undefined?step.output_display:step.output" in ws
