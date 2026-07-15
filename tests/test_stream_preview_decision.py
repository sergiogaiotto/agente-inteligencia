"""Backlog 4 do arco condicional (36.2.1): linha DECISAO × streaming.

O stream é por EVENTO de step (não por token): a exposição real era o
`output_preview[:300]` do `agent_done` — resposta curta de classificador cabe
inteira, linha inclusa. O preview passa pelo mesmo regime de apresentação
(strip com gate de contrato); steps/trace seguem crus e o gate lê o completo.
Todas as superfícies de stream (pipelines e workspace) consomem esta emissão.
"""
import pytest

SKILL_MD = """# Triagem
## Decisions
```json
{ "escalar": ["sim", "não"], "severidade": ["baixa", "média", "alta"] }
```
"""


class TestDisplayPreviewDoStream:
    @pytest.mark.asyncio
    async def test_preview_sem_a_linha_quando_ha_contrato(self, monkeypatch):
        import app.agents.engine as eng

        async def _agent(_id):
            return {"id": _id, "skill_id": "sk-1"}

        async def _skill(_id):
            return {"id": _id, "raw_content": SKILL_MD}

        monkeypatch.setattr(eng, "_topo_agent", _agent)
        monkeypatch.setattr(eng.skills_repo, "find_by_id", _skill)
        got = await eng._display_preview("Caso grave.\nDECISAO: escalar=sim", "ag-1")
        assert got == "Caso grave."

    @pytest.mark.asyncio
    async def test_sem_contrato_preview_cru_e_fail_safe(self, monkeypatch):
        import app.agents.engine as eng

        async def _sem_skill(_id):
            return {"id": _id, "skill_id": ""}

        monkeypatch.setattr(eng, "_topo_agent", _sem_skill)
        txt = "Análise.\nDecisão: aprovado o crédito"
        assert await eng._display_preview(txt, "ag-x") == txt  # prosa fica (gate duplo)

        async def _boom(_id):
            raise RuntimeError("db off")

        monkeypatch.setattr(eng, "_topo_agent", _boom)
        raw = "Resposta.\nDECISAO: escalar=sim"
        assert await eng._display_preview(raw, "ag-1") == raw  # fail-safe

    @pytest.mark.asyncio
    async def test_limite_de_300_aplicado_apos_o_strip(self, monkeypatch):
        import app.agents.engine as eng

        async def _agent(_id):
            return {"id": _id, "skill_id": "sk-1"}

        async def _skill(_id):
            return {"id": _id, "raw_content": SKILL_MD}

        monkeypatch.setattr(eng, "_topo_agent", _agent)
        monkeypatch.setattr(eng.skills_repo, "find_by_id", _skill)
        longa = ("x" * 400) + "\nDECISAO: escalar=sim"
        got = await eng._display_preview(longa, "ag-1")
        assert len(got) == 300 and "DECISAO" not in got


def test_emissao_do_agent_done_usa_o_preview_de_apresentacao():
    from pathlib import Path
    src = Path("app/agents/engine.py").read_text(encoding="utf-8")
    i = src.find('"type": "agent_done"')
    assert i != -1
    bloco = src[i:i + 900]
    assert "await _display_preview(result.get(\"output\", \"\"), agent_id)" in bloco
