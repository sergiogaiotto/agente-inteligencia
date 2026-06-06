"""RAG em pipeline — fix de 3 causas (2026-06-06).

Bug relatado (verbatim do operador): o pipeline "router → Retenção" devolvia
{tips:[], source_chunks:[]} APESAR das KBs corretas e populadas. As bases
existiam mas não eram consultadas "com inteligência".

Diagnóstico (3 causas em camadas):

- **Causa 1 (DECISIVA, arquitetural, ANTIGA):** o engine PULAVA o retrieval para
  QUALQUER agente com `pipeline_context` — então um especialista downstream
  ficava CEGO às KBs que declarou em `evidence_policy.sources`. A correção é
  opt-in ESTRITO: só auto-recupera quando o agente DECLARA sources (lista
  populada). Pipelines sem declaração ficam idênticos → zero regressão. Lógica
  isolada na função pura `_pipeline_should_self_retrieve`.

- **Causa 2 (config, do PR de grounding):** o router (entry, sem KB, triagem,
  grounding-strict) RECUSAVA por falta de evidência e essa recusa virava o
  `pipeline_context` (lixo) do especialista. Correção: router é DISPATCHER →
  ISENTO da recusa de grounding (subagent/aobd seguem estritos). Aplicado no
  call site reusando o caminho do escape hatch (allow_general_knowledge), sem
  tocar na função pura `_grounding_guard`.

- **Causa 3 (qualidade de recall):** BM25 usava `plainto_tsquery('simple', …)`
  (sem stemming, exigindo TODOS os termos) → perguntas naturais não casavam.
  Correção: 'portuguese' (stemming) + termos unidos por OR + NULLIF p/ blindar
  query só-stopword. Constante de módulo `_BM25_TSQUERY_PT`. A coluna `tsv`
  passa a ser gerada com 'portuguese' (DDL + migração idempotente).

Convenção (cf. test_grounding_by_default.py): NÃO chamamos execute_interaction
inteiro (pesado, depende de DB+LLM) — testamos as peças isoladas (funções puras)
+ assertions de wiring por leitura de fonte. A integração no FSM é exercida no
smoke manual / homolog.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from app.agents import engine
from app.agents.engine import _pipeline_should_self_retrieve
from app.evidence.runtime import _BM25_TSQUERY_PT

ENGINE_SRC = Path("app/agents/engine.py").read_text(encoding="utf-8")
RUNTIME_SRC = Path("app/evidence/runtime.py").read_text(encoding="utf-8")
DATABASE_SRC = Path("app/core/database.py").read_text(encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════
# Causa 1 — _pipeline_should_self_retrieve (função pura)
# ═══════════════════════════════════════════════════════════════════
class TestPipelineShouldSelfRetrieve:
    def test_pipeline_com_sources_declaradas_recupera(self):
        """O caso do bug: especialista em pipeline COM KBs declaradas → recupera."""
        assert _pipeline_should_self_retrieve(
            has_pipeline_context=True, declared_sources=True, skip_evidence=False,
        ) is True

    def test_pipeline_sem_sources_preserva_skip(self):
        """Sem declaração de sources → comportamento ANTIGO (não recupera). Zero regressão."""
        assert _pipeline_should_self_retrieve(
            has_pipeline_context=True, declared_sources=False, skip_evidence=False,
        ) is False

    def test_skip_evidence_vence_mesmo_com_sources(self):
        """skip_evidence explícito (require_evidence=0) NÃO é sobreposto pela auto-recuperação."""
        assert _pipeline_should_self_retrieve(
            has_pipeline_context=True, declared_sources=True, skip_evidence=True,
        ) is False

    def test_sem_pipeline_context_nao_se_aplica(self):
        """Fora de pipeline a função é irrelevante (retrieval normal segue seu curso)."""
        assert _pipeline_should_self_retrieve(
            has_pipeline_context=False, declared_sources=True, skip_evidence=False,
        ) is False

    def test_keyword_only(self):
        """Assinatura keyword-only evita troca acidental de flags na call site."""
        sig = inspect.signature(_pipeline_should_self_retrieve)
        kinds = {p.kind for p in sig.parameters.values()}
        assert kinds == {inspect.Parameter.KEYWORD_ONLY}


# ═══════════════════════════════════════════════════════════════════
# Causa 1 — wiring no engine (gate + declaração de sources)
# ═══════════════════════════════════════════════════════════════════
class TestCausa1Wiring:
    def test_gate_usa_helper_puro(self):
        """A gate de retrieval chama o helper (não duplica a lógica inline)."""
        assert "_pipeline_should_self_retrieve(" in ENGINE_SRC

    def test_declares_sources_exige_lista_populada(self):
        """Declaração = lista populada (None legado e [] bloqueado NÃO contam)."""
        assert (
            "_declares_sources = isinstance(_allowed_sources, list) "
            "and len(_allowed_sources) > 0" in ENGINE_SRC
        )

    def test_gate_combina_pipeline_e_own_rag(self):
        """Pula só quando há pipeline_context E NÃO é own-rag (ou skip_evidence)."""
        assert "if (pipeline_context and not _pipeline_own_rag) or skip_evidence:" in ENGINE_SRC

    def test_allowed_sources_calculado_antes_da_gate(self):
        """_allowed_sources precisa vir ANTES da gate (a gate depende dele)."""
        i_alloc = ENGINE_SRC.index('_allowed_sources = _ev_policy.get("sources")')
        i_gate = ENGINE_SRC.index("_pipeline_own_rag = _pipeline_should_self_retrieve(")
        assert i_alloc < i_gate

    def test_observabilidade_pipeline_own_rag_no_span(self):
        """Trace expõe se o agente fez RAG próprio em pipeline (diagnóstico)."""
        assert 'set_attribute("evidence.pipeline_own_rag", _pipeline_own_rag)' in ENGINE_SRC


# ═══════════════════════════════════════════════════════════════════
# Causa 1 — retrieval_query (query limpa em pipeline)
# ═══════════════════════════════════════════════════════════════════
class TestRetrievalQueryThreading:
    def test_execute_interaction_tem_param_retrieval_query(self):
        sig = inspect.signature(engine.execute_interaction)
        assert "retrieval_query" in sig.parameters
        assert sig.parameters["retrieval_query"].default is None

    def test_search_query_cai_para_user_input(self):
        """retrieval_query None → usa user_input (fora de pipeline, sem mudança)."""
        assert "_search_query = retrieval_query or user_input" in ENGINE_SRC

    def test_pipeline_passa_pergunta_original(self):
        """execute_pipeline passa a pergunta ORIGINAL (user_input), não o current_input prefixado."""
        assert "retrieval_query=user_input," in ENGINE_SRC
        # o input que circula no laço é o prefixado — NÃO deve ser o usado na busca
        assert "user_input=current_input," in ENGINE_SRC

    def test_busca_e_rerank_usam_a_mesma_query_limpa(self):
        """retrieve e rerank compartilham _search_query (consistência)."""
        assert "retriever.search(" in ENGINE_SRC
        assert "reranker.rerank(_search_query," in ENGINE_SRC


# ═══════════════════════════════════════════════════════════════════
# Causa 2 — router isento da recusa de grounding (wiring no call site)
# ═══════════════════════════════════════════════════════════════════
class TestCausa2RouterGroundingExempt:
    def test_router_exempt_por_kind(self):
        assert "_router_grounding_exempt = (str(agent.get(\"kind\") or \"\").lower() == \"router\")" in ENGINE_SRC

    def test_exempt_combina_escape_hatch_e_router(self):
        """Isenção = escape hatch (allow_general_knowledge) OU router."""
        assert "_grounding_exempt = _gk_allowed or _router_grounding_exempt" in ENGINE_SRC

    def test_guard_recebe_exempt_nao_so_gk(self):
        """O call site passa a isenção combinada para a função pura (que fica intacta)."""
        assert "allow_general_knowledge=_grounding_exempt," in ENGINE_SRC

    def test_could_refuse_respeita_exempt(self):
        """A consulta cara de tool-grounding só roda quando ainda PODE recusar."""
        assert "_grounding_strict_eff and not _grounding_exempt" in ENGINE_SRC

    def test_trace_expoe_router_exempt(self):
        """Auditoria: o trace registra se foi o router que escapou (vs. escape hatch)."""
        assert '"router_exempt": _router_grounding_exempt,' in ENGINE_SRC

    def test_grounding_guard_permanece_puro(self):
        """A função pura NÃO ganhou conhecimento de 'kind' (isenção é só no call site)."""
        src_guard = inspect.getsource(engine._grounding_guard)
        assert "router" not in src_guard.lower()
        assert "kind" not in src_guard.lower()

    @pytest.mark.parametrize("kind,exempt", [
        ("router", True),
        ("Router", True),   # case-insensitive
        ("ROUTER", True),
        ("subagent", False),
        ("aobd", False),
        ("", False),
        (None, False),
    ])
    def test_logica_de_isencao_por_kind(self, kind, exempt):
        """Replica a expressão do call site: só 'router' (qualquer caixa) é isento."""
        computed = (str(kind or "").lower() == "router")
        assert computed is exempt


# ═══════════════════════════════════════════════════════════════════
# Causa 3 — BM25 portuguese + OR + NULLIF (constante de módulo)
# ═══════════════════════════════════════════════════════════════════
class TestCausa3Bm25Tsquery:
    def test_constante_usa_portuguese(self):
        assert "'portuguese'" in _BM25_TSQUERY_PT
        assert "'simple'" not in _BM25_TSQUERY_PT

    def test_constante_converte_and_para_or(self):
        """regexp_replace troca o ' & ' (AND implícito) por ' | ' (OR) → casa QUALQUER termo."""
        assert "regexp_replace(" in _BM25_TSQUERY_PT
        assert "' & '" in _BM25_TSQUERY_PT
        assert "' | '" in _BM25_TSQUERY_PT

    def test_constante_tem_nullif_anti_stopword(self):
        """NULLIF blinda query só-stopword (vira NULL → não casa, sem erro de sintaxe)."""
        assert "NULLIF(" in _BM25_TSQUERY_PT

    def test_constante_parametriza_dollar1(self):
        """$1 (a query do usuário) é parâmetro — sem injeção; só a FORMA é interpolada."""
        assert "$1" in _BM25_TSQUERY_PT

    def test_constante_monta_to_tsquery(self):
        assert _BM25_TSQUERY_PT.startswith("to_tsquery('portuguese'")

    def test_bm25_search_usa_a_constante(self):
        """_bm25_search referencia a constante (rank + WHERE), sem reescrever a expressão."""
        assert "tsq = _BM25_TSQUERY_PT" in RUNTIME_SRC
        # nenhum resíduo do 'simple' antigo na SQL VIVA. A docstring ainda cita o
        # 'simple' (explica o fix), por isso casamos a FORMA parametrizada ($1) —
        # que só existe na query executável, nunca no texto explicativo (que usa …).
        assert "plainto_tsquery('simple', $1)" not in RUNTIME_SRC
        assert "plainto_tsquery('portuguese', $1)" in _BM25_TSQUERY_PT

    def test_bm25_aplica_tsq_em_rank_e_where(self):
        """A expressão entra tanto no ts_rank_cd quanto no operador @@ (WHERE)."""
        assert "ts_rank_cd(ec.tsv, {tsq})" in RUNTIME_SRC
        assert "ec.tsv @@ {tsq}" in RUNTIME_SRC


# ═══════════════════════════════════════════════════════════════════
# Causa 3 — DDL + migração idempotente da coluna gerada `tsv`
# ═══════════════════════════════════════════════════════════════════
class TestCausa3DatabaseTsv:
    def test_ddl_gera_tsv_em_portuguese(self):
        assert (
            "tsv TSVECTOR GENERATED ALWAYS AS "
            "(to_tsvector('portuguese', coalesce(text, ''))) STORED," in DATABASE_SRC
        )

    def test_ddl_nao_usa_simple(self):
        """A coluna gerada não pode mais ser 'simple' (recall PT)."""
        assert "to_tsvector('simple'" not in DATABASE_SRC

    def test_migracao_guardada_por_simple(self):
        """A migração só dispara se a expressão ATUAL ainda referenciar 'simple' (idempotente)."""
        assert "pg_get_expr(d.adbin, d.adrelid) LIKE '%simple%'" in DATABASE_SRC

    def test_migracao_recria_coluna_gerada(self):
        """Coluna GENERATED não aceita ALTER da expressão → DROP + ADD regenera as linhas."""
        assert "ALTER TABLE evidence_chunks DROP COLUMN tsv;" in DATABASE_SRC
        assert (
            "ALTER TABLE evidence_chunks ADD COLUMN tsv TSVECTOR\n"
            "          GENERATED ALWAYS AS (to_tsvector('portuguese', "
            "coalesce(text, ''))) STORED;" in DATABASE_SRC
        )

    def test_migracao_recria_indice_gin(self):
        """O índice GIN é dropado antes e recriado depois (a coluna some no meio)."""
        assert "DROP INDEX IF EXISTS idx_evidence_chunks_tsv;" in DATABASE_SRC
        assert "CREATE INDEX idx_evidence_chunks_tsv ON evidence_chunks USING GIN (tsv);" in DATABASE_SRC


# ═══════════════════════════════════════════════════════════════════
# Anti-drift de paleta — minhas adições não introduzem roxo
# ═══════════════════════════════════════════════════════════════════
def test_areas_novas_sem_roxo():
    """Trava local de paleta (ortogonal a test_palette_no_purple_tones)."""
    pat = re.compile(r"\b(violet|fuchsia|purple)-\d")
    for src in (ENGINE_SRC, RUNTIME_SRC, DATABASE_SRC):
        assert not pat.search(src)
