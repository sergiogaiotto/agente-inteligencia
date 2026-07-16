"""Golden Dataset — edição e exclusão de casos (QA E2E 2026-07-16).

A UI só tinha "+ Novo caso": corrigir um typo num caso exigia apagar via API e
recriar (perdendo o id). Agora: PUT /gold-cases/{id} no backend + botões de
editar/excluir por linha na UI, com modo de edição visível no form.

Integridade histórica (por que editar/excluir é seguro aqui): cada eval_run
guarda o gold_hash do conjunto que avaliou — mudar um caso NÃO reescreve runs
passados, e comparações contra um conjunto mutado são RECUSADAS pelo guard de
hash em vez de mentir.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.core.database as db
import app.routes.dashboard as dash


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(dash.router)
    return TestClient(app, raise_server_exceptions=False)


_PAYLOAD = {
    "input_text": "minha internet caiu de novo",
    "expected_output": "Diagnostico e chamado com SLA.",
    "case_type": "normal",
    "expected_state": "Recommend",
    "dataset_version": "nexus-v1",
    "category": "tecnico",
    "weight": 2.0,
    "expected_pattern": "(diagn|chamado)",
    "red_flags": ["senha", "cartao"],
}


class TestPutGoldCase:
    def test_atualiza_e_serializa_red_flags(self, client, monkeypatch):
        """red_flags viaja como lista e persiste como JSON string (coluna TEXT) —
        mesma coerção do create; sem ela o asyncpg recusa a lista crua."""
        captured = {}

        async def _update(case_id, payload):
            captured["id"] = case_id
            captured["payload"] = payload
            return {"id": case_id, **payload}
        monkeypatch.setattr(db.gold_cases_repo, "update", _update)

        r = client.put("/api/v1/gold-cases/gc-1", json=_PAYLOAD)
        assert r.status_code == 200, r.text
        assert captured["id"] == "gc-1"
        assert captured["payload"]["input_text"] == _PAYLOAD["input_text"]
        assert captured["payload"]["weight"] == 2.0
        # serializado, não lista crua
        assert isinstance(captured["payload"]["red_flags"], str)
        assert json.loads(captured["payload"]["red_flags"]) == ["senha", "cartao"]

    def test_404_para_caso_inexistente(self, client, monkeypatch):
        async def _update(case_id, payload):
            return None
        monkeypatch.setattr(db.gold_cases_repo, "update", _update)
        r = client.put("/api/v1/gold-cases/fantasma", json=_PAYLOAD)
        assert r.status_code == 404

    def test_payload_invalido_e_422(self, client):
        r = client.put("/api/v1/gold-cases/gc-1", json={"weight": "não-numero"})
        assert r.status_code == 422


class TestUI:
    @pytest.fixture(scope="class")
    def html(self) -> str:
        return (Path(__file__).resolve().parent.parent / "app" / "templates"
                / "pages" / "harness.html").read_text(encoding="utf-8")

    def test_botoes_por_linha_nao_abrem_o_drawer(self, html):
        """@click.stop: editar/excluir não podem disparar o openCase da linha."""
        for tid in ("gold-edit", "gold-delete"):
            i = html.index(f'data-testid="{tid}"')
            bloco = html[i - 300: i + 100]
            assert "@click.stop" in bloco, f"{tid} sem @click.stop dispara o drawer"

    def test_modo_edicao_visivel_e_cancelavel(self, html):
        """Sem o badge, o operador não sabe se está criando ou alterando."""
        assert 'data-testid="gold-editing-badge"' in html
        i = html.index('data-testid="gold-editing-badge"')
        assert "cancelar" in html[i: i + 700]

    def test_botao_salvar_muda_de_rotulo(self, html):
        i = html.index('data-testid="gold-save"')
        bloco = html[i: i + 400]
        assert "Salvar alterações" in bloco and "Adicionar ao Golden Dataset" in bloco

    def test_submit_faz_put_na_edicao_e_post_na_criacao(self, html):
        i = html.index("async createGoldCase()")
        fn = html[i: i + 1600]
        assert "api.put('/api/v1/gold-cases/' + this.editingGoldId" in fn
        assert "api.post('/api/v1/gold-cases'" in fn

    def test_excluir_confirma_com_a_verdade_do_hash(self, html):
        """A confirmação diz o que REALMENTE acontece: runs passados não mudam
        (gold_hash); próximos runs usam o conjunto sem o caso."""
        i = html.index("async deleteGoldCase(")
        fn = html[i: i + 900]
        assert "uiConfirm" in fn and "danger: true" in fn
        assert "hash do conjunto avaliado" in fn

    def test_novo_caso_abre_form_limpo(self, html):
        """Abrir pelo '+ Novo caso' depois de uma edição não pode herdar o modo
        edição — senão o próximo 'criar' sobrescreve o caso anterior."""
        fn = html[html.index("toggleGoldForm(){"): html.index("toggleGoldForm(){") + 300]
        assert "resetGoldForm()" in fn
        j = html.index("resetGoldForm(){")
        assert "this.editingGoldId = null" in html[j: j + 200]
