"""User pediu (2026-06-01): inspirado nas métricas que a tela de Skill
mostra (caracteres, tokens, custo/chamada, custo/mês), criar painel
similar na tela do Agente — mas indo além porque o Agente tem dados
que a Skill sozinha não tem: modelo configurado, histórico real,
conexões no mesh, capacidades.

Stack desta PR:
- `app/core/llm_pricing.py`: tabela de preços por modelo (USD/1M tokens)
- `app/core/token_estimator.py`: estimativa via tiktoken + fallback char/4
- `app/routes/agents.py:agent_diagnostics`: endpoint que combina tudo
- `app/templates/pages/agent_form.html`: painel rico em Step Revisão
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ─── llm_pricing ────────────────────────────────────────────────────


class TestLlmPricingTableHasNewEntries:
    """Esta PR adicionou modelos faltantes (gpt-4.1, claude-3-5-sonnet,
    gemini, openai_public aliases) à PRICING table existente. Não
    duplicou a API — usa o `compute_cost(provider, model, in, out)`
    legado em USD/1k tokens."""

    def test_gpt_41_added(self):
        from app.core.llm_pricing import get_pricing
        p = get_pricing("openai", "gpt-4.1")
        assert p is not None
        assert p["input"] > 0 and p["output"] > 0

    def test_gpt_41_mini_cheaper_than_full(self):
        """Sanity econômico: mini é mais barato que o full."""
        from app.core.llm_pricing import get_pricing
        mini = get_pricing("openai", "gpt-4.1-mini")
        full = get_pricing("openai", "gpt-4.1")
        assert mini["input"] < full["input"]

    def test_oss_models_zero(self):
        from app.core.llm_pricing import get_pricing
        for entry in [
            ("openai_public", "gpt-oss-120b"),
            ("gpt-oss-120b", "openai/gpt-oss-120b"),
            ("gpt-oss-20b", "openai/gpt-oss-20b"),
        ]:
            p = get_pricing(*entry)
            assert p is not None and p["input"] == 0 and p["output"] == 0

    def test_compute_cost_consistency(self):
        """compute_cost devolve USD/1k tokens (não 1M). gpt-4o: $0.0025/1k input,
        $0.01/1k output → 1k+500 = 0.0025 + 0.005 = 0.0075"""
        from app.core.llm_pricing import compute_cost
        cost = compute_cost("openai", "gpt-4o", 1000, 500)
        assert abs(cost - 0.0075) < 1e-9


# ─── token_estimator ────────────────────────────────────────────────


class TestTokenEstimator:
    def test_empty_returns_zero(self):
        from app.core.token_estimator import estimate_tokens
        assert estimate_tokens("") == 0
        assert estimate_tokens(None) == 0  # type: ignore

    def test_content_returns_positive(self):
        from app.core.token_estimator import estimate_tokens
        # Heurística char/4: "ola mundo" (9 chars) ≈ 2 tokens
        n = estimate_tokens("ola mundo")
        assert n >= 1

    def test_long_text_scales(self):
        """Texto 10x maior gera ~10x tokens (ordem de magnitude)."""
        from app.core.token_estimator import estimate_tokens
        small = estimate_tokens("x" * 100)
        big = estimate_tokens("x" * 1000)
        assert big >= small * 5  # tiktoken pode comprimir; só checa ordem


# ─── endpoint /diagnostics ──────────────────────────────────────────


@pytest.fixture
def diag_client():
    """TestClient para o router de agents (auth bypass via dependency override)."""
    from app.routes.agents import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _patch_diag_deps(
    monkeypatch,
    *,
    agent=None,
    skill=None,
    interactions=None,
    mesh_in=None,
    mesh_out=None,
):
    """Patcha todas as deps do endpoint /diagnostics."""
    async def fake_agent_find(aid):
        return agent if (agent and aid == agent["id"]) else None

    async def fake_skill_find(sid):
        return skill if (skill and sid == skill.get("id")) else None

    async def fake_int_find_all(agent_id=None, limit=200, **_):
        return interactions or []

    async def fake_mesh_find_all(source_agent_id=None, target_agent_id=None, limit=50, **_):
        if source_agent_id:
            return mesh_out or []
        if target_agent_id:
            return mesh_in or []
        return []

    # Endpoint usa `agents_repo` direto (módulo agents) e os outros via
    # `from app.core.database import ...` LAZY (dentro do handler), então
    # patchar via app.core.database é o caminho correto.
    monkeypatch.setattr("app.routes.agents.agents_repo.find_by_id", fake_agent_find)
    monkeypatch.setattr("app.core.database.skills_repo.find_by_id", fake_skill_find)
    monkeypatch.setattr("app.core.database.interactions_repo.find_all", fake_int_find_all)
    monkeypatch.setattr("app.core.database.mesh_repo.find_all", fake_mesh_find_all)


class TestDiagnosticsEndpoint:
    def test_404_for_unknown_agent(self, diag_client, monkeypatch):
        _patch_diag_deps(monkeypatch, agent=None)
        r = diag_client.get("/api/v1/agents/missing/diagnostics")
        assert r.status_code == 404

    def test_oss_model_zero_cost(self, diag_client, monkeypatch):
        """gpt-oss-120b é free → cost_per_call e cost_per_month = 0."""
        agent = {
            "id": "a1", "name": "X", "model": "gpt-oss-120b",
            "llm_provider": "openai_public", "system_prompt": "Você é X.",
            "skill_id": None, "accepts_images": False, "accepts_documents": False,
        }
        _patch_diag_deps(monkeypatch, agent=agent)
        r = diag_client.get("/api/v1/agents/a1/diagnostics")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["cost"]["model"] == "gpt-oss-120b"
        assert body["cost"]["cost_per_call_usd"] == 0
        assert body["cost"]["cost_per_month_usd"] == 0

    def test_multimodal_warning_when_text_only_model_accepts_images(self, diag_client, monkeypatch):
        """REGRESSÃO: agente com accepts_images=true + modelo text-only
        deve sinalizar incompat (já capturado pelo PR #248 no routing —
        este painel torna isso VISÍVEL no form de edição antes do erro)."""
        agent = {
            "id": "a1", "name": "X", "model": "gpt-oss-120b",
            "llm_provider": "openai_public", "system_prompt": "Você é X.",
            "skill_id": None,
            "accepts_images": True,  # gpt-oss-120b é text-only → warning
            "accepts_documents": False,
        }
        _patch_diag_deps(monkeypatch, agent=agent)
        r = diag_client.get("/api/v1/agents/a1/diagnostics")
        body = r.json()
        assert body["capabilities"]["multimodal_warning"] is True
        assert body["capabilities"]["is_multimodal_model"] is False

    def test_multimodal_ok_when_model_supports_images(self, diag_client, monkeypatch):
        """gpt-4.1 + accepts_images=true → sem warning."""
        agent = {
            "id": "a1", "name": "X", "model": "gpt-4.1",
            "llm_provider": "openai_public", "system_prompt": "Você é X.",
            "skill_id": None, "accepts_images": True, "accepts_documents": False,
        }
        _patch_diag_deps(monkeypatch, agent=agent)
        r = diag_client.get("/api/v1/agents/a1/diagnostics")
        body = r.json()
        assert body["capabilities"]["multimodal_warning"] is False
        assert body["capabilities"]["is_multimodal_model"] is True

    def test_no_history_yields_null_perf_metrics(self, diag_client, monkeypatch):
        """Sem interactions nos últimos 30d → success_rate=None (UI mostra placeholder)."""
        agent = {
            "id": "a1", "name": "X", "model": "gpt-4o-mini",
            "llm_provider": "openai", "system_prompt": "x",
            "skill_id": None, "accepts_images": False, "accepts_documents": False,
        }
        _patch_diag_deps(monkeypatch, agent=agent, interactions=[])
        r = diag_client.get("/api/v1/agents/a1/diagnostics")
        body = r.json()
        assert body["performance"]["interactions_last_30d"] == 0
        assert body["performance"]["success_rate"] is None
        assert body["performance"]["p50_latency_ms"] is None

    def test_history_computes_p50_p95_and_success_rate(self, diag_client, monkeypatch):
        """Com 10 interactions recentes → p50/p95 calculados, success_rate."""
        agent = {
            "id": "a1", "name": "X", "model": "gpt-4o-mini",
            "llm_provider": "openai", "system_prompt": "x",
            "skill_id": None, "accepts_images": False, "accepts_documents": False,
        }
        now = datetime.now(timezone.utc)
        # 10 interactions: durações 100..1000ms; 7 em Recommend (70% success)
        interactions = []
        for i in range(10):
            interactions.append({
                "id": f"i{i}",
                "agent_id": "a1",
                "duration_ms": (i + 1) * 100,
                "state": "Recommend" if i < 7 else "Refuse",
                "created_at": now - timedelta(hours=i),
            })
        _patch_diag_deps(monkeypatch, agent=agent, interactions=interactions)
        r = diag_client.get("/api/v1/agents/a1/diagnostics")
        body = r.json()
        perf = body["performance"]
        assert perf["interactions_last_30d"] == 10
        assert perf["success_rate"] == 0.7
        assert perf["drift_pct"] == round(1 - 0.7, 3)
        # p50 ≈ mediano (500ms), p95 ≈ topo (~900ms)
        assert 400 <= perf["p50_latency_ms"] <= 600
        assert perf["p95_latency_ms"] >= 800

    def test_mesh_counts_in_and_out(self, diag_client, monkeypatch):
        agent = {
            "id": "a1", "name": "X", "model": "gpt-4o-mini",
            "llm_provider": "openai", "system_prompt": "x",
            "skill_id": None, "accepts_images": False, "accepts_documents": False,
        }
        _patch_diag_deps(
            monkeypatch, agent=agent,
            mesh_in=[{"id": "c1"}, {"id": "c2"}],
            mesh_out=[{"id": "c3"}],
        )
        r = diag_client.get("/api/v1/agents/a1/diagnostics")
        body = r.json()
        assert body["capabilities"]["mesh_upstream_count"] == 2
        assert body["capabilities"]["mesh_downstream_count"] == 1

    def test_health_score_bounded_0_100(self, diag_client, monkeypatch):
        agent = {
            "id": "a1", "name": "X", "model": "gpt-4o-mini",
            "llm_provider": "openai", "system_prompt": "x",
            "skill_id": None, "accepts_images": False, "accepts_documents": False,
        }
        _patch_diag_deps(monkeypatch, agent=agent)
        r = diag_client.get("/api/v1/agents/a1/diagnostics")
        body = r.json()
        score = body["health"]["score"]
        assert 0 <= score <= 100

    def test_logandclose_counts_as_success(self, diag_client, monkeypatch):
        """LogAndClose é sucesso (paridade engine.py:2147, que loga Recommend
        E LogAndClose como nível 'success'). Agente determinístico que fecha
        em LogAndClose não pode ser penalizado no success_rate."""
        agent = {
            "id": "a1", "name": "X", "model": "gpt-4o-mini",
            "llm_provider": "openai", "system_prompt": "x",
            "skill_id": None, "accepts_images": False, "accepts_documents": False,
        }
        now = datetime.now(timezone.utc)
        # 10 interactions: 4 Recommend + 3 LogAndClose (=7 sucesso) + 3 Refuse
        states = ["Recommend"] * 4 + ["LogAndClose"] * 3 + ["Refuse"] * 3
        interactions = [
            {"id": f"i{i}", "agent_id": "a1", "duration_ms": 100,
             "state": s, "created_at": now - timedelta(hours=i)}
            for i, s in enumerate(states)
        ]
        _patch_diag_deps(monkeypatch, agent=agent, interactions=interactions)
        body = diag_client.get("/api/v1/agents/a1/diagnostics").json()
        # 7/10, não 4/10 — LogAndClose conta
        assert body["performance"]["success_rate"] == 0.7

    def test_health_unreliable_below_min_sample(self, diag_client, monkeypatch):
        """< 20 interações → reliable=False (UI: cor neutra + 'provisório').
        Expõe min_sample e sample_size pro frontend montar o aviso."""
        agent = {
            "id": "a1", "name": "X", "model": "gpt-4o-mini",
            "llm_provider": "openai", "system_prompt": "x",
            "skill_id": None, "accepts_images": False, "accepts_documents": False,
        }
        now = datetime.now(timezone.utc)
        interactions = [
            {"id": f"i{i}", "agent_id": "a1", "duration_ms": 100,
             "state": "Refuse", "created_at": now - timedelta(hours=i)}
            for i in range(5)
        ]
        _patch_diag_deps(monkeypatch, agent=agent, interactions=interactions)
        h = diag_client.get("/api/v1/agents/a1/diagnostics").json()["health"]
        assert h["reliable"] is False
        assert h["min_sample"] == 20
        assert h["sample_size"] == 5

    def test_health_reliable_at_min_sample(self, diag_client, monkeypatch):
        """>= 20 interações → reliable=True (amostra suficiente)."""
        agent = {
            "id": "a1", "name": "X", "model": "gpt-4o-mini",
            "llm_provider": "openai", "system_prompt": "x",
            "skill_id": None, "accepts_images": False, "accepts_documents": False,
        }
        now = datetime.now(timezone.utc)
        interactions = [
            {"id": f"i{i}", "agent_id": "a1", "duration_ms": 100,
             "state": "Recommend", "created_at": now - timedelta(minutes=i)}
            for i in range(20)
        ]
        _patch_diag_deps(monkeypatch, agent=agent, interactions=interactions)
        h = diag_client.get("/api/v1/agents/a1/diagnostics").json()["health"]
        assert h["reliable"] is True
        assert h["sample_size"] == 20

    def test_health_unreliable_when_no_history(self, diag_client, monkeypatch):
        """Cold-start (0 interações): score pode dar 100 (só o 'potencial' de
        config), mas reliable=False evita pintar verde com falsa confiança."""
        agent = {
            "id": "a1", "name": "X", "model": "gpt-4o-mini",
            "llm_provider": "openai", "system_prompt": "x",
            "skill_id": "sk1", "accepts_images": False, "accepts_documents": False,
        }
        skill = {"id": "sk1", "name": "S", "raw_content": "# S"}
        _patch_diag_deps(monkeypatch, agent=agent, skill=skill, interactions=[])
        h = diag_client.get("/api/v1/agents/a1/diagnostics").json()["health"]
        assert h["reliable"] is False
        assert h["sample_size"] == 0


# ─── UI smoke do template ────────────────────────────────────────────


class TestAgentFormDiagnosticsPanel:
    def test_state_has_diagnostics_fields(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "agent_form.html").read_text(encoding="utf-8")
        assert "diagnostics: null," in src
        assert "diagnosticsLoading: false," in src

    def test_load_method_calls_load_diagnostics(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "agent_form.html").read_text(encoding="utf-8")
        assert "this.loadDiagnostics();" in src
        assert "async loadDiagnostics()" in src

    def test_panel_only_renders_in_edit_mode(self):
        """O painel inteiro está dentro de `<template x-if="isEdit">`.
        Busca o <div> visível (com class text-[11px] font-bold) — a string
        'Diagnóstico do Agente' aparece em vários lugares (comentários,
        JS state), então marker visual é mais robusto."""
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "agent_form.html").read_text(encoding="utf-8")
        # Marker do título VISÍVEL do painel (não confundir com comentários)
        marker = 'uppercase tracking-widest">Diagnóstico do Agente'
        assert marker in src
        idx = src.index(marker)
        # No bloco anterior (~500 chars) deve estar `x-if="isEdit"`
        assert 'x-if="isEdit"' in src[max(0, idx - 500):idx]

    def test_panel_shows_cost_performance_capabilities_health(self):
        """Os 4 blocos do diagnóstico estão presentes na UI."""
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "agent_form.html").read_text(encoding="utf-8")
        # Headers das 4 sub-seções (case sensitive — checa título visível)
        assert "Custo · " in src
        assert "Performance · últimos 30d" in src
        assert "Capacidades" in src
        assert "Health Score" in src

    def test_multimodal_warning_block_present(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "agent_form.html").read_text(encoding="utf-8")
        # Avisa quando model text-only + accepts_images
        assert "multimodal_warning" in src
        assert "Incompatibilidade multimodal" in src

    def test_health_score_neutral_color_when_unreliable(self):
        """Amostra insuficiente (!reliable) → barra/número em cor neutra
        (surface), não verde/âmbar/vermelho — evita falso-alarme no cold-start."""
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "agent_form.html").read_text(encoding="utf-8")
        assert "!diagnostics.health.reliable ? 'bg-surface-300'" in src
        assert "!diagnostics.health.reliable ? 'text-surface-400'" in src

    def test_health_score_provisional_warning(self):
        """Aviso 'provisório' gated por !reliable, mostrando sample_size/min_sample."""
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "agent_form.html").read_text(encoding="utf-8")
        assert 'x-show="!diagnostics.health.reliable"' in src
        assert "provisório" in src
        assert "diagnostics.health.sample_size" in src
        assert "diagnostics.health.min_sample" in src

    def test_health_score_scale_legend(self):
        """Legenda da escala torna o racional explícito (não caixa-preta)."""
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "agent_form.html").read_text(encoding="utf-8")
        assert "saudável" in src
        assert "atenção" in src
        assert "&lt;50" in src
        assert "crítico" in src


# ─── UI smoke: "IA, me ajude!" no Composer (slice atual) ──────────────


def _form_html() -> str:
    from pathlib import Path
    return (
        Path(__file__).resolve().parent.parent
        / "app" / "templates" / "pages" / "agent_form.html"
    ).read_text(encoding="utf-8")


class TestComposerAiAssist:
    """O Composer ganhou um botão "IA, me ajude!" que preenche um RASCUNHO dos
    campos (missão/regras/fallback/regra de ouro) via /api/v1/wizard/compose,
    ancorado no catálogo real. Smoke estrutural do HTML/JS Alpine."""

    def test_botao_ia_me_ajude_presente(self):
        src = _form_html()
        assert "IA, me ajude!" in src

    def test_estado_composeai_inicializado(self):
        src = _form_html()
        # State Alpine com os 4 campos do painel.
        assert "composeAi: { intent: '', loading: false, error: '', isDraft: false }" in src

    def test_metodo_compose_chama_endpoint(self):
        src = _form_html()
        assert "async composeWithAi()" in src
        assert "/api/v1/wizard/compose" in src

    def test_envia_catalogo_real_para_ancorar(self):
        """Manda nomes de skills + agentes reais — mitiga alucinação de destino."""
        src = _form_html()
        assert "(this.availableSkills || []).map(s => s.name)" in src
        assert "(this.availableAgents || []).map(a => a.name)" in src

    def test_preenche_como_rascunho_nao_aplica(self):
        """Preenche mission.* e marca isDraft; NÃO chama applyMissionComposer
        nem mexe direto em form.system_prompt (humano no loop)."""
        src = _form_html()
        # bloco do composeWithAi seta o rascunho
        start = src.index("async composeWithAi()")
        end = src.index("composeAi.loading = false;", start)
        block = src[start:end + 60]
        assert "this.mission = {" in block
        assert "this.composeAi.isDraft = true;" in block
        assert "applyMissionComposer" not in block
        assert "form.system_prompt" not in block

    def test_afford_rascunho_revise_visivel(self):
        """Affordance 'rascunho da IA — revise' gated por isDraft."""
        src = _form_html()
        assert "rascunho da IA — revise" in src
        assert 'x-show="composeAi.isDraft' in src

    def test_input_de_intencao_presente(self):
        src = _form_html()
        assert 'x-model="composeAi.intent"' in src
        assert "composeAiPlaceholder()" in src

    def test_loading_e_erro_no_painel(self):
        src = _form_html()
        # botão desabilita enquanto carrega; erro acionável renderiza
        assert 'composeAi.loading || !composeAi.intent.trim()' in src
        assert 'x-text="composeAi.error"' in src

    def test_parsed_false_avisa_texto_livre(self):
        """Quando a IA não devolve JSON (parsed=false), avisa o usuário."""
        src = _form_html()
        assert "d.parsed === false" in src

    def test_reset_do_rascunho_ao_abrir_composer(self):
        """openComposer limpa o estado do painel IA (não vaza rascunho antigo)."""
        src = _form_html()
        # Âncora na DEFINIÇÃO do método (com '{'), não no @click="openComposer()".
        start = src.index("openComposer() {")
        block = src[start:start + 400]
        assert "this.composeAi = { intent: '', loading: false, error: '', isDraft: false };" in block
