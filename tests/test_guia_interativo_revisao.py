"""Blindagem da revisão completa do Guia Interativo (v15.2.0).

Trava as três frentes que a revisão de 2026-06-21 corrigiu, para impedir
regressão:

1. GUIA DOS MÓDULOS — novos módulos de subsistemas que antes não existiam
   (Catálogo, Saúde dos Modelos, Plataforma Externa/DAST, Ferramentas MCP).
2. TOUR — passos + ids de highlight para Infra/Histórico/Configurações e para
   o chip de Saúde dos Modelos (sem o id, o getElementById do tour falha).
3. AJUDA DESTA PÁGINA — o objeto/modal `helpContent` legado (morto, com termos
   obsoletos AOBD/AR/SA, Qdrant, Sabiá/Maritaca, DeepAgent) foi REMOVIDO; e o
   _pagePathMap passa a apontar as sub-rotas do Catálogo para suas keys V2
   dedicadas (antes caíam no genérico 'catalog').
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MOD = ROOT / "app" / "static" / "js" / "module-guide.js"
HELP = ROOT / "app" / "static" / "js" / "help-content.js"
BASE = ROOT / "app" / "templates" / "layouts" / "base.html"


# ── 1. Guia dos Módulos ──────────────────────────────────────────────
def test_new_subsystem_modules_present():
    src = MOD.read_text(encoding="utf-8")
    for mid in ("catalog", "model_health", "external_platform", "tools_mcp"):
        assert f"id: '{mid}'" in src, f"módulo de subsistema ausente: {mid}"


def test_module_count_grew_past_seventeen():
    """A revisão adicionou 4 módulos (17 → 21). O piso histórico era 17."""
    src = MOD.read_text(encoding="utf-8")
    n = len(re.findall(r"^\s{4}id: '", src, re.M))
    assert n >= 21, f"esperava >=21 módulos após a revisão, achei {n}"


def test_module_guide_section_labels_consistent():
    """external_platform agrupa em 'Catálogo' (acento) e tools_mcp em
    'Integração' (acento) — divergência de acento criaria grupos duplicados."""
    src = MOD.read_text(encoding="utf-8")
    assert "section: 'Catálogo'" in src
    assert "section: 'Integração'" in src
    assert "section: 'Catalogo'" not in src   # sem-acento = grupo fantasma
    assert "section: 'Integracao'" not in src


# ── 2. Tour ──────────────────────────────────────────────────────────
def test_tour_steps_for_infra_history_settings_health():
    txt = BASE.read_text(encoding="utf-8")
    for el in ("tour-nav-infra", "tour-nav-history", "tour-nav-settings", "tour-model-health"):
        # cada um precisa aparecer 2x: o id no DOM + o passo no array tourSteps
        assert txt.count(el) >= 2, f"tour/id incompleto para {el} (esperava DOM + passo)"


def test_nav_divs_have_tour_ids():
    """Sem id no <div> da nav, o highlight do tour (getElementById) não acende."""
    txt = BASE.read_text(encoding="utf-8")
    assert 'id="tour-nav-infra"' in txt
    assert 'id="tour-nav-history"' in txt
    assert 'id="tour-nav-settings"' in txt
    assert 'id="tour-model-health"' in txt


def test_tour_releases_no_unimplemented_rollback_claim():
    """O passo de Releases não pode prometer 'rollback automático' (não existe)."""
    txt = BASE.read_text(encoding="utf-8")
    assert "rollback automático" not in txt


# ── 3. Ajuda desta página: legacy removido + page map dedicado ───────
def test_legacy_helpcontent_object_removed():
    txt = BASE.read_text(encoding="utf-8")
    assert "helpContent" not in txt, "objeto/modal legacy helpContent ainda presente"
    assert "helpOpen" not in txt, "flag/modal legacy helpOpen ainda presente"
    assert 'x-text="helpData' not in txt, "modal legacy helpData ainda referenciado"


def test_legacy_obsolete_terms_gone_from_base():
    """Termos obsoletos viviam só no helpContent legado — não podem sobreviver."""
    txt = BASE.read_text(encoding="utf-8")
    for term in ("DeepAgent", "LangGraph", "Sabiá-3", "Maritaca AI", "AOBD (Orquestrador"):
        assert term not in txt, f"termo obsoleto remanescente em base.html: {term}"


def test_pagepathmap_catalog_subpages_use_dedicated_keys():
    txt = BASE.read_text(encoding="utf-8")
    assert "'/catalog/queue': 'catalog_queue'" in txt
    assert "'/catalog/inventory': 'catalog_inventory'" in txt
    assert "'/catalog/stewardship': 'catalog_stewardship'" in txt
    # federation e infra precisam estar mapeados (antes caíam em 'dashboard')
    assert "'/federation': 'federation'" in txt
    assert "'/infra': 'infra'" in txt


# ── Coerência de fidelidade técnica (regressões factuais corrigidas) ──
def test_no_qdrant_as_live_backend_in_help():
    """pgvector é o backend único desde a Onda Q — Qdrant não é mais 'ativo'."""
    src = HELP.read_text(encoding="utf-8")
    assert "Qdrant ou pgvector" not in src
    assert "pgvector ou Qdrant" not in src
    assert "RAG_VECTOR_BACKEND" not in src


def test_harness_endpoint_corrected_in_module_guide():
    """O endpoint real de execução é /eval-runs/execute (não /harness/run)."""
    src = MOD.read_text(encoding="utf-8")
    assert "/api/v1/eval-runs/execute" in src
    assert "/api/v1/harness/run" not in src
