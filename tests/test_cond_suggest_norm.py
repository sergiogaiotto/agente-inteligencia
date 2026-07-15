"""Gêmeo do tradutor NL→Jinja × normalização (fecha o major do review 2026-07-15).

O #617 criou as vars `*_norm` e o catálogo passou a dizer "prefira input_norm",
mas o tradutor era gêmeo esquecido: o prompt só ensinava `*_lower`, o repair não
cobria as `_norm`, e NADA normalizava o literal — `'não reconheço' in input_norm`
passava por toda a validação e NUNCA casava (a var é sempre normalizada no
runtime). Aqui: prompt ensina `_norm`, `_TEXT_TARGETS` cobre as 3, e o repair
determinístico `normalize_norm_literals` sela o literal.
"""
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agents.conditional_suggest import (
    _TEXT_TARGETS,
    build_suggest_messages,
    normalize_norm_literals,
    repair_unquoted_literals,
)

PG = Path("app/templates/pages/mesh_flow.html")


# ─── prompt ensina _norm (a contradição prompt × catálogo morreu) ─────────────

def test_prompt_prefere_norm_com_termo_normalizado():
    msgs = build_suggest_messages("se mencionar pix", [
        {"name": "input_norm", "type": "str", "desc": "..."},
    ])
    sys = msgs[0]["content"]
    assert "input_norm" in sys
    assert "SEM acento" in sys
    # exemplo com termo já normalizado (ensina pelo exemplo, não só pela regra)
    assert "'nao reconhec' in input_norm" in sys
    # legado despriorizado, não banido (exprs antigas seguem válidas)
    assert "legado" in sys


# ─── _TEXT_TARGETS cobre as _norm (repair de literal sem aspas) ───────────────

def test_repair_unquoted_cobre_norm():
    assert set(("input_norm", "output_norm", "text_norm")) <= set(_TEXT_TARGETS)
    got = repair_unquoted_literals("pix in input_norm", {"input_norm"})
    assert got == "'pix' in input_norm"


# ─── normalize_norm_literals (o selo determinístico) ──────────────────────────

def test_literal_acentuado_contra_norm_e_normalizado():
    got = normalize_norm_literals("'não reconheço' in input_norm")
    assert got == "'nao reconheco' in input_norm"


def test_idempotente_e_multiplos():
    expr = "'nao reconheco' in input_norm or 'Fraude' in output_norm"
    once = normalize_norm_literals(expr)
    assert once == "'nao reconheco' in input_norm or 'fraude' in output_norm"
    assert normalize_norm_literals(once) == once


def test_nao_toca_legado_nem_inputs():
    # *_lower/text_all são acento-exato POR CONTRATO — normalizar mudaria a semântica
    expr = "'não reconheço' in input_lower and inputs.tier == 'Gold'"
    assert normalize_norm_literals(expr) == expr


def test_casefold_no_literal():
    # espelha o casefold do runtime (ß → ss)
    assert normalize_norm_literals("'Hauptstraße' in text_norm") == "'hauptstrasse' in text_norm"


# ─── endpoint aplica o repair (LLM mockado, padrão do test_mesh_rule_translator) ──

@pytest.fixture
def client(monkeypatch):
    from app.routes.mesh import router
    from app.core.auth import require_user

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: {"id": "u1", "role": "comum"}

    async def _fake_resolve(task_type, has_image=False):
        return ("openai", "gpt-x")
    monkeypatch.setattr("app.llm_routing.resolve_llm_for_task", _fake_resolve)
    return app, monkeypatch


def test_endpoint_normaliza_literal_da_ia(client):
    app, monkeypatch = client

    async def _fake_llm(messages, provider, model, *, route, temperature=None, response_format=None):
        # a IA seguiu o catálogo ("prefira input_norm") mas manteve o acento —
        # exatamente o cenário do major: regra sempre-falsa sem o repair.
        return ("'não reconheço' in input_norm", provider, model)
    monkeypatch.setattr("app.routes.wizard._wizard_llm_complete", _fake_llm)

    c = TestClient(app)
    r = c.post("/api/v1/mesh/connections/suggest-conditional",
               json={"description": "quando o cliente disser não reconheço"})
    body = r.json()
    assert body["expr"] == "'nao reconheco' in input_norm"
    assert body["valid"] is True


# ─── caminho manual: aviso 1-clique no template ───────────────────────────────

def test_template_avisa_literal_acentuado_contra_norm():
    src = PG.read_text(encoding="utf-8")
    # detector no exprWarnings + corretor 1-clique (padrão did-you-mean)
    assert "input_norm|output_norm|text_norm" in src
    assert "fixLit(w)" in src
    assert "kind: 'lit'" in src
    # fixLit não deixa veredito velho mentir (lição do #619)
    assert "fixLit(w) {" in src


def test_template_strip_accents_espelha_python():
    src = PG.read_text(encoding="utf-8")
    # \p{M} espelha unicodedata.combining (a faixa U+0300–036F não cobria
    # outros scripts); ß→ss espelha o casefold do runtime.
    assert "replace(/\\p{M}/gu, '')" in src
    assert "replace(/ß/g, 'ss')" in src
