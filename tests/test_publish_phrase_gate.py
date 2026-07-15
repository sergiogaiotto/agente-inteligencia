"""Frases-Prova como teste de regressão REAL na publicação (36.0.0).

Fecha a promessa do editor de fluxo: as frases seladas em cada aresta
condicional rodam contra o avaliador do RUNTIME (`_build_conditional_context`
+ `_eval_conditional`, com `decision.*` extraída do output simulado) no ato de
publicar o pipeline no Catálogo. Rotas testadas em test_catalog_api
(TestPublishPhraseGate); aqui, os avaliadores.
"""
import pytest

from app.agents.engine import evaluate_test_phrases_for_edge
from app.catalog import pipeline_defs


# ─── evaluate_test_phrases_for_edge (espelho do runtime) ──────────────────────

class TestEvaluateEdgePhrases:
    @pytest.mark.asyncio
    async def test_frase_de_pergunta_passa_e_reprova(self):
        res = await evaluate_test_phrases_for_edge(
            source_id="s1", expr="'pix' in input_norm",
            phrases=[
                {"text": "fiz um PIX ontem", "where": "input", "expect": True},
                {"text": "quero segunda via", "where": "input", "expect": True},
                {"text": "quero segunda via", "where": "input", "expect": False},
            ],
        )
        assert [r["passed"] for r in res] == [True, False, True]
        assert res[1]["got"] is False and res[1]["expect"] is True

    @pytest.mark.asyncio
    async def test_frase_de_resposta_extrai_decision_do_source(self, monkeypatch):
        import app.agents.engine as eng

        async def _agent(_id):
            return {"id": _id, "skill_id": "sk-1"}

        async def _skill(_id):
            return {"id": _id, "raw_content": (
                "# T\n## Decisions\n```json\n{ \"escalar\": [\"sim\", \"não\"] }\n```\n")}

        monkeypatch.setattr(eng, "_topo_agent", _agent)
        monkeypatch.setattr(eng.skills_repo, "find_by_id", _skill)
        res = await evaluate_test_phrases_for_edge(
            source_id="s1", expr="decision.escalar == 'sim'",
            phrases=[{"text": "Caso grave.\nDECISAO: escalar=sim", "where": "output", "expect": True}],
        )
        assert res[0]["passed"] is True

    @pytest.mark.asyncio
    async def test_erro_de_expr_e_fail_closed_com_erro_anexado(self):
        res = await evaluate_test_phrases_for_edge(
            source_id="s1", expr="output_length > 'texto'",  # int > str estoura
            phrases=[{"text": "qualquer", "where": "output", "expect": True}],
        )
        assert res[0]["passed"] is False
        assert res[0]["error"]

    @pytest.mark.asyncio
    async def test_frase_vazia_e_ignorada(self):
        res = await evaluate_test_phrases_for_edge(
            source_id="s1", expr="has_output",
            phrases=[{"text": "   ", "where": "input", "expect": True}, None],
        )
        assert res == []


# ─── evaluate_pipeline_test_phrases (varredura do subgrafo) ───────────────────

class TestEvaluatePipelinePhrases:
    @pytest.mark.asyncio
    async def test_varre_so_condicionais_com_frases_e_nomeia_agentes(self, monkeypatch):
        async def _sub(_pid):
            return {
                "root_agent_id": "a1",
                "nodes": [{"id": "a1", "name": "Triagem"}, {"id": "a2", "name": "Fraude"}],
                "edges": [
                    {"id": "e1", "source": "a1", "target": "a2", "type": "conditional",
                     "config": {"expr": "'pix' in input_norm", "test_phrases": [
                         {"text": "fiz um pix", "where": "input", "expect": True},
                         {"text": "segunda via", "where": "input", "expect": True},  # reprova
                     ]}},
                    {"id": "e2", "source": "a1", "target": "a2", "type": "sequential", "config": {}},
                    {"id": "e3", "source": "a1", "target": "a2", "type": "conditional",
                     "config": {"expr": "has_output"}},  # sem frases → fora
                ],
            }
        monkeypatch.setattr(pipeline_defs, "_build_subgraph", _sub)
        rep = await pipeline_defs.evaluate_pipeline_test_phrases("pip-1")
        assert rep["evaluated"] == 2 and rep["passed"] == 1
        assert len(rep["failing"]) == 1
        f = rep["failing"][0]
        assert (f["source_name"], f["target_name"]) == ("Triagem", "Fraude")
        assert f["edge_id"] == "e1" and f["text"] == "segunda via"

    @pytest.mark.asyncio
    async def test_pipeline_sem_frases_relatorio_vazio(self, monkeypatch):
        async def _sub(_pid):
            return {"root_agent_id": "a1", "nodes": [], "edges": []}
        monkeypatch.setattr(pipeline_defs, "_build_subgraph", _sub)
        rep = await pipeline_defs.evaluate_pipeline_test_phrases("pip-1")
        assert rep == {"evaluated": 0, "passed": 0, "failing": []}


# ─── rodapé do editor promete o que agora EXISTE ──────────────────────────────

def test_rodape_promete_regressao_real():
    from pathlib import Path
    src = Path("app/templates/pages/mesh_flow.html").read_text(encoding="utf-8")
    assert "teste de regressão do roteamento — reprovação bloqueia a publicação" in src
