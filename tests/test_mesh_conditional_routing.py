"""User pediu (2026-06-01): como definir as regras do "Conditional" no AI Mesh?

Antes deste PR, o tipo "Conditional" no form do mesh era decorativo —
gravava `connection_type='conditional'` + `config="{}"` mas:
- UI: nenhum campo para definir a regra
- Runtime: `_resolve_ordered_chain` ignorava `connection_type`,
  executando BFS puro

Agora:
- UI: textarea aparece quando type=conditional, com placeholder Jinja
- Storage: `config={"expr": "<jinja>"}` em mesh_repo
- Runtime: `_should_skip_conditional` avalia expr contra output do
  agente upstream antes de cada execução em `execute_pipeline`

Política de erro: **fail-open**. Qualquer falha (config malformado,
expr inválida, exception no Jinja) loga warning e NÃO skipa — agente
executa, operador vê o erro em errors.log. Melhor executar errado do
que perder dado silenciosamente.
"""
from __future__ import annotations

import json
import logging

import pytest


# ─── _eval_conditional (helper isolado) ─────────────────────────────


class TestEvalConditional:
    def test_simple_true_expression(self):
        from app.agents.engine import _eval_conditional
        ctx = {"output": "tem imagem", "output_lower": "tem imagem", "final_state": "Recommend"}
        assert _eval_conditional("'imagem' in output_lower", ctx) is True

    def test_simple_false_expression(self):
        from app.agents.engine import _eval_conditional
        ctx = {"output": "só texto", "output_lower": "só texto", "final_state": "Recommend"}
        assert _eval_conditional("'imagem' in output_lower", ctx) is False

    def test_final_state_match(self):
        from app.agents.engine import _eval_conditional
        ctx = {"output": "x", "output_lower": "x", "final_state": "Refuse"}
        assert _eval_conditional("final_state == 'Refuse'", ctx) is True

    def test_length_check(self):
        from app.agents.engine import _eval_conditional
        ctx = {"output": "x" * 100, "output_lower": "x" * 100, "final_state": ""}
        assert _eval_conditional("output|length > 50", ctx) is True
        assert _eval_conditional("output|length > 500", ctx) is False

    def test_invalid_expr_raises(self):
        """Sintaxe inválida → exception (caller decide se fail-open ou closed)."""
        from app.agents.engine import _eval_conditional
        with pytest.raises(Exception):
            _eval_conditional("this is not valid jinja !!!", {"output": ""})

    def test_undefined_variable_does_not_explode(self):
        """ChainableUndefined: usar var não definida não levanta — útil
        para exprs de operadores menos familiarizados (não derruba pipeline
        por typo)."""
        from app.agents.engine import _eval_conditional
        ctx = {"output": "x", "output_lower": "x", "final_state": ""}
        # `intent` não existe — deve avaliar para falsy sem explodir
        result = _eval_conditional("intent == 'image'", ctx)
        assert result is False


# ─── _should_skip_conditional ────────────────────────────────────────


class TestShouldSkipConditional:
    @pytest.mark.asyncio
    async def test_non_conditional_connection_never_skips(self, monkeypatch):
        from app.agents import engine as eng

        async def fake_find_all(source_agent_id=None, **_):
            return [{
                "source_agent_id": "a",
                "target_agent_id": "b",
                "connection_type": "sequential",
                "config": "{}",
            }]
        monkeypatch.setattr("app.core.database.mesh_repo.find_all", fake_find_all)

        out = await eng._should_skip_conditional(
            source_id="a", target_id="b", last_output="x", last_final_state="",
        )
        assert out is False

    @pytest.mark.asyncio
    async def test_conditional_without_expr_passes(self, monkeypatch):
        """config={} (sem expr) = sempre passa (equivalente a sequencial)."""
        from app.agents import engine as eng

        async def fake_find_all(source_agent_id=None, **_):
            return [{
                "source_agent_id": "a",
                "target_agent_id": "b",
                "connection_type": "conditional",
                "config": "{}",
            }]
        monkeypatch.setattr("app.core.database.mesh_repo.find_all", fake_find_all)

        out = await eng._should_skip_conditional(
            source_id="a", target_id="b", last_output="x", last_final_state="",
        )
        assert out is False

    @pytest.mark.asyncio
    async def test_conditional_expr_true_does_not_skip(self, monkeypatch):
        from app.agents import engine as eng

        async def fake_find_all(source_agent_id=None, **_):
            return [{
                "source_agent_id": "a",
                "target_agent_id": "b",
                "connection_type": "conditional",
                "config": json.dumps({"expr": "'imagem' in output_lower"}),
            }]
        monkeypatch.setattr("app.core.database.mesh_repo.find_all", fake_find_all)

        out = await eng._should_skip_conditional(
            source_id="a", target_id="b",
            last_output="tem imagem aqui", last_final_state="",
        )
        assert out is False  # expr=true → não skipa

    @pytest.mark.asyncio
    async def test_conditional_expr_false_skips(self, monkeypatch):
        """REGRESSÃO do bug central: quando a regra avalia false, agente é
        pulado (passthrough)."""
        from app.agents import engine as eng

        async def fake_find_all(source_agent_id=None, **_):
            return [{
                "source_agent_id": "a",
                "target_agent_id": "b",
                "connection_type": "conditional",
                "config": json.dumps({"expr": "'imagem' in output_lower"}),
            }]
        monkeypatch.setattr("app.core.database.mesh_repo.find_all", fake_find_all)

        out = await eng._should_skip_conditional(
            source_id="a", target_id="b",
            last_output="só texto puro", last_final_state="",
        )
        assert out is True

    @pytest.mark.asyncio
    async def test_malformed_config_fails_open_and_logs(self, monkeypatch, caplog):
        """Config JSON inválido → fail-open + log warning."""
        from app.agents import engine as eng

        async def fake_find_all(source_agent_id=None, **_):
            return [{
                "source_agent_id": "a",
                "target_agent_id": "b",
                "connection_type": "conditional",
                "config": "{ not valid json",
            }]
        monkeypatch.setattr("app.core.database.mesh_repo.find_all", fake_find_all)

        with caplog.at_level(logging.WARNING, logger="app.agents.engine"):
            out = await eng._should_skip_conditional(
                source_id="a", target_id="b", last_output="x", last_final_state="",
            )

        assert out is False  # fail-open
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "mesh.conditional" in events

    @pytest.mark.asyncio
    async def test_eval_error_fails_open_and_logs(self, monkeypatch, caplog):
        """Expr com erro de sintaxe → fail-open + log warning."""
        from app.agents import engine as eng

        async def fake_find_all(source_agent_id=None, **_):
            return [{
                "source_agent_id": "a",
                "target_agent_id": "b",
                "connection_type": "conditional",
                "config": json.dumps({"expr": "syntax !!!"}),
            }]
        monkeypatch.setattr("app.core.database.mesh_repo.find_all", fake_find_all)

        with caplog.at_level(logging.WARNING, logger="app.agents.engine"):
            out = await eng._should_skip_conditional(
                source_id="a", target_id="b", last_output="x", last_final_state="",
            )

        assert out is False
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "mesh.conditional" in events
        # Extra carrega a expr e o tipo do erro pra troubleshooting
        rec = next(r for r in caplog.records if getattr(r, "event", None) == "mesh.conditional")
        assert getattr(rec, "expr", None) == "syntax !!!"


# ─── Override "o roteador mandou" (2026-06-06) ───────────────────────
#
# Bug real (interaction 46f280de-3413-426f-9b9e-f8524bc30dbb): o AR
# "Pesquisador A" roteou CERTO para o SA Rentab (output: "Encaminhar a
# pergunta ao agente **Rentab**."), mas a expr condicional casa `input_lower`
# e a pergunta do usuário ("o que posso fazer para gerar receita") não tem
# nenhuma keyword de Rentab → expr=false → Rentab era `skipped_conditional`
# (1/3 executados). A decisão semântica do roteador (que NOMEIA o alvo no
# output) precisa vencer o heurístico de keywords. Camada complementar à
# correção de morfologia (PR #295): aquela casa o vocabulário do usuário;
# esta honra a escolha explícita do roteador quando o vocabulário não casa.


class TestOutputNamesTarget:
    """Helper isolado: o texto do upstream NOMEIA explicitamente o alvo?"""

    def test_exact_name_in_prose_matches(self):
        from app.agents.engine import _output_names_target
        assert _output_names_target(
            "Encaminhar a pergunta ao agente Rentab.", "Rentab"
        ) is True

    def test_name_in_markdown_bold_matches(self):
        """Caso REAL do bug: o roteador escreve o nome do alvo em **negrito**."""
        from app.agents.engine import _output_names_target
        assert _output_names_target(
            "Encaminhar a pergunta ao agente **Rentab**.", "Rentab"
        ) is True

    def test_case_insensitive(self):
        from app.agents.engine import _output_names_target
        assert _output_names_target("vou chamar o RENTAB agora", "rentab") is True

    def test_accent_insensitive_both_directions(self):
        """'Retenção' casa 'retencao' (LLM às vezes tira o acento) e vice-versa."""
        from app.agents.engine import _output_names_target
        assert _output_names_target("delego para Retencao", "Retenção") is True
        assert _output_names_target("delego para Retenção", "Retencao") is True

    def test_word_boundary_no_substring_match(self):
        """'Rentab' NÃO casa dentro de 'rentabilidade' — só naming EXPLÍCITO."""
        from app.agents.engine import _output_names_target
        assert _output_names_target("falaremos sobre rentabilidade", "Rentab") is False

    def test_short_name_does_not_trigger(self):
        """Nome curto (< 3 chars) é ambíguo (casaria qualquer texto) → não dispara."""
        from app.agents.engine import _output_names_target
        assert _output_names_target("a resposta é a", "A") is False

    def test_empty_output_or_name_is_false(self):
        from app.agents.engine import _output_names_target
        assert _output_names_target("", "Rentab") is False
        assert _output_names_target("qualquer coisa", "") is False
        assert _output_names_target(None, None) is False


class TestRouterNamedTargetOverride:
    """Integração no `_should_skip_conditional`: o naming explícito do roteador
    no output vence a expr (mesmo expr=false)."""

    def _patch_conn(self, monkeypatch, expr):
        async def fake_find_all(source_agent_id=None, **_):
            return [{
                "source_agent_id": "router",
                "target_agent_id": "rentab",
                "connection_type": "conditional",
                "config": json.dumps({"expr": expr}),
            }]
        monkeypatch.setattr("app.core.database.mesh_repo.find_all", fake_find_all)

    @pytest.mark.asyncio
    async def test_router_named_target_runs_despite_false_expr(self, monkeypatch):
        """REGRESSÃO do bug 46f280de: expr casa input_lower e a pergunta não tem
        keyword, MAS o roteador nomeou Rentab no output → deve RODAR (não skipar)."""
        from app.agents import engine as eng
        # expr REAL gravada na pipeline reparada (casa input_lower)
        self._patch_conn(monkeypatch, "'rentab' in input_lower or 'financ' in input_lower")
        out = await eng._should_skip_conditional(
            source_id="router", target_id="rentab",
            last_output="Encaminhar a pergunta ao agente **Rentab**.",
            last_final_state="LogAndClose",
            user_input="o que posso fazer para gerar receita",  # SEM keyword
            target_name="Rentab",
        )
        assert out is False

    @pytest.mark.asyncio
    async def test_target_not_named_and_false_expr_still_skips(self, monkeypatch):
        """Override não relaxa demais: sem naming do alvo + expr false → skip."""
        from app.agents import engine as eng
        self._patch_conn(monkeypatch, "'rentab' in input_lower")
        out = await eng._should_skip_conditional(
            source_id="router", target_id="rentab",
            last_output="Encaminhar a pergunta ao agente **Retenção**.",  # nomeia OUTRO
            last_final_state="LogAndClose",
            user_input="o que posso fazer para gerar receita",
            target_name="Rentab",
        )
        assert out is True

    @pytest.mark.asyncio
    async def test_override_logs_rationale(self, monkeypatch, caplog):
        """Observabilidade: a decisão de NÃO skipar por naming fica logada."""
        from app.agents import engine as eng
        self._patch_conn(monkeypatch, "'nada' in input_lower")
        with caplog.at_level(logging.INFO, logger="app.agents.engine"):
            out = await eng._should_skip_conditional(
                source_id="router", target_id="rentab",
                last_output="... ao agente Rentab.",
                last_final_state="", user_input="x", target_name="Rentab",
            )
        assert out is False
        decisions = [getattr(r, "decision", None) for r in caplog.records]
        assert "run_not_skip" in decisions

    @pytest.mark.asyncio
    async def test_naming_irrelevant_for_sequential(self, monkeypatch):
        """Sequential nunca skipa — naming não muda nada (sem regressão)."""
        from app.agents import engine as eng

        async def fake_find_all(source_agent_id=None, **_):
            return [{
                "source_agent_id": "router", "target_agent_id": "rentab",
                "connection_type": "sequential", "config": "{}",
            }]
        monkeypatch.setattr("app.core.database.mesh_repo.find_all", fake_find_all)
        out = await eng._should_skip_conditional(
            source_id="router", target_id="rentab",
            last_output="ao agente Rentab", last_final_state="",
            target_name="Rentab",
        )
        assert out is False


class TestOutputRoutesToTarget:
    """Camada restrita (2026-06-06 — bug 'Doc Analise → Imagem'): o upstream
    NOMEIA o alvo EM CONTEXTO DE ROTEAMENTO? (verbo/'agente'/seta antes do nome,
    não a palavra solta em prosa). Mata o falso-positivo de nome-palavra-comum."""

    def test_routing_verb_before_name_is_true(self):
        from app.agents.engine import _output_routes_to_target
        assert _output_routes_to_target(
            "Encaminhar a pergunta ao agente Imagem.", "Imagem"
        ) is True

    def test_arrow_routing_is_true(self):
        from app.agents.engine import _output_routes_to_target
        assert _output_routes_to_target("Decisao de roteamento: -> Imagem", "Imagem") is True

    def test_agente_noun_cue_is_true(self):
        from app.agents.engine import _output_routes_to_target
        assert _output_routes_to_target("delego para o agente Documentos", "Documentos") is True

    def test_markdown_bold_routing_is_true(self):
        from app.agents.engine import _output_routes_to_target
        assert _output_routes_to_target("Encaminhar ao agente **Rentab**.", "Rentab") is True

    def test_prose_mention_without_cue_is_false(self):
        """O caso REAL do bug: o roteador resume um .pptx e a prosa contém a
        palavra 'imagem' descrevendo figuras — NÃO é roteamento ao SA Imagem."""
        from app.agents.engine import _output_routes_to_target
        prosa = '- **Foco em valor** (imagem "Dinheiro com preenchimento solido")'
        assert _output_routes_to_target(prosa, "Imagem") is False

    def test_bare_name_in_prose_is_false(self):
        from app.agents.engine import _output_routes_to_target
        assert _output_routes_to_target("o relatorio tem uma imagem na capa", "Imagem") is False

    def test_word_boundary_inherited_from_names_target(self):
        """Herda a fronteira de palavra: 'Rentab' não casa em 'rentabilidade'."""
        from app.agents.engine import _output_routes_to_target
        assert _output_routes_to_target("rotear sobre rentabilidade", "Rentab") is False

    def test_empty_inputs_are_false(self):
        from app.agents.engine import _output_routes_to_target
        assert _output_routes_to_target("", "Imagem") is False
        assert _output_routes_to_target("encaminhar ao agente X", "") is False
        assert _output_routes_to_target(None, None) is False


class TestRouterNamedTargetCapabilityVeto:
    """Override 'o roteador mandou' é VETADO quando há anexo de tipo que o alvo
    nomeado NÃO trata (SA de imagem + documento). Capacidade é autoridade."""

    _DOC = {"name": "Apresentacao.pptx",
            "type": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}
    _IMG = {"name": "foto.png", "type": "image/png"}

    def _patch_conn(self, monkeypatch, expr, target_id="imagem"):
        async def fake_find_all(source_agent_id=None, **_):
            return [{
                "source_agent_id": "router", "target_agent_id": target_id,
                "connection_type": "conditional",
                "config": json.dumps({"expr": expr}),
            }]
        monkeypatch.setattr("app.core.database.mesh_repo.find_all", fake_find_all)

    @pytest.mark.asyncio
    async def test_image_sa_vetoed_with_document_attachment(self, monkeypatch):
        """Roteador NOMEIA Imagem (contexto de roteamento) MAS chega documento e
        Imagem só aceita imagem → veto → skip (não roda handler errado)."""
        from app.agents import engine as eng
        self._patch_conn(monkeypatch, "'imagem' in input_lower or 'foto' in input_lower")
        out = await eng._should_skip_conditional(
            source_id="router", target_id="imagem",
            last_output="Encaminhar ao agente Imagem.",
            last_final_state="LogAndClose",
            user_input="lista o conteudo em bullets",
            target_name="Imagem",
            attachments=[self._DOC],
            target_accepts_documents=False,
            target_accepts_images=True,
        )
        assert out is True

    @pytest.mark.asyncio
    async def test_image_sa_runs_when_image_attached(self, monkeypatch):
        """Mesma rota, anexo é imagem → Imagem TRATA → não veta → roda."""
        from app.agents import engine as eng
        self._patch_conn(monkeypatch, "'nada' in input_lower")
        out = await eng._should_skip_conditional(
            source_id="router", target_id="imagem",
            last_output="Encaminhar ao agente Imagem.",
            last_final_state="LogAndClose",
            user_input="o que tem aqui",
            target_name="Imagem",
            attachments=[self._IMG],
            target_accepts_documents=False,
            target_accepts_images=True,
        )
        assert out is False

    @pytest.mark.asyncio
    async def test_named_target_no_attachment_still_runs(self, monkeypatch):
        """Sem anexo o veto não se aplica — naming explícito ainda vence a expr."""
        from app.agents import engine as eng
        self._patch_conn(monkeypatch, "'nada' in input_lower")
        out = await eng._should_skip_conditional(
            source_id="router", target_id="imagem",
            last_output="Encaminhar ao agente Imagem.",
            last_final_state="", user_input="x",
            target_name="Imagem",
        )
        assert out is False

    @pytest.mark.asyncio
    async def test_veto_logs_capability_mismatch(self, monkeypatch, caplog):
        from app.agents import engine as eng
        self._patch_conn(monkeypatch, "'nada' in input_lower")
        with caplog.at_level(logging.INFO, logger="app.agents.engine"):
            out = await eng._should_skip_conditional(
                source_id="router", target_id="imagem",
                last_output="Encaminhar ao agente Imagem.",
                last_final_state="", user_input="x",
                target_name="Imagem",
                attachments=[self._DOC],
                target_accepts_documents=False, target_accepts_images=True,
            )
        assert out is True
        reasons = [getattr(r, "reason", None) for r in caplog.records]
        assert "capability_mismatch" in reasons


class TestDocAnaliseImagemProseLeakRegression:
    """REGRESSÃO end-to-end do item reportado (2026-06-06): pptx + 'lista o
    conteudo em bullets' NÃO deve acionar o SA Imagem. O roteador cita 'imagem'
    em PROSA (resumindo figuras) — sem cue de roteamento → Imagem é PULADO.
    Documentos continua rodando pela autoridade de CAPACIDADE."""

    _PPTX = {"name": "Apresentacao_DIA.pptx",
             "type": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}
    _ROUTER_PROSE = (
        "Apresentacao - Conteudo resumido em topicos\n"
        "- **Foco em valor** (imagem \"Dinheiro com preenchimento solido\")\n"
        "- **Transformacao de pessoas** (imagem \"Crescimento Comercial\")\n"
        "- **Evolucao dos habilitadores** (imagem \"Banco de dados\")"
    )
    _IMG_EXPR = ("'imagem' in input_lower or 'envia' in input_lower or "
                 "'foto' in input_lower or 'png' in input_lower or "
                 "'jpeg' in input_lower or 'similar' in input_lower")
    _DOC_EXPR = ("'document' in input_lower or 'documento' in input_lower or "
                 "'pdf' in input_lower or 'arquivo' in input_lower or "
                 "'similar' in input_lower")

    def _patch_conn(self, monkeypatch, target_id, expr):
        async def fake_find_all(source_agent_id=None, **_):
            return [{
                "source_agent_id": "router", "target_agent_id": target_id,
                "connection_type": "conditional",
                "config": json.dumps({"expr": expr}),
            }]
        monkeypatch.setattr("app.core.database.mesh_repo.find_all", fake_find_all)

    @pytest.mark.asyncio
    async def test_imagem_skipped_on_document_with_prose_mention(self, monkeypatch):
        from app.agents import engine as eng
        self._patch_conn(monkeypatch, "imagem", self._IMG_EXPR)
        out = await eng._should_skip_conditional(
            source_id="router", target_id="imagem",
            last_output=self._ROUTER_PROSE,
            last_final_state="LogAndClose",
            user_input="lista o conteudo em bullets",
            target_name="Imagem",
            attachments=[self._PPTX],
            target_accepts_documents=False,
            target_accepts_images=True,
        )
        assert out is True  # PULADO (antes do fix: False → rodava)

    @pytest.mark.asyncio
    async def test_documentos_still_runs_via_capability(self, monkeypatch):
        from app.agents import engine as eng
        self._patch_conn(monkeypatch, "documentos", self._DOC_EXPR)
        out = await eng._should_skip_conditional(
            source_id="router", target_id="documentos",
            last_output=self._ROUTER_PROSE,
            last_final_state="LogAndClose",
            user_input="lista o conteudo em bullets",
            target_name="Documentos",
            attachments=[self._PPTX],
            target_accepts_documents=True,
            target_accepts_images=False,
        )
        assert out is False  # roda (override de capacidade)


# ─── Source smoke: UI tem o campo, backend topology expõe config ──


class TestUiAndBackendWiring:
    def test_mesh_html_has_condition_expr_field(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "mesh.html").read_text(encoding="utf-8")
        # Textarea x-show condicional
        assert "x-show=\"connForm.connection_type === 'conditional'\"" in src
        assert "x-model=\"connForm.condition_expr\"" in src
        # Helpers mostrados na UI
        assert "output_lower" in src
        assert "final_state" in src

    def test_save_connection_serializes_expr_when_conditional(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "mesh.html").read_text(encoding="utf-8")
        # Payload usa expr só quando type=conditional
        assert "if (this.connForm.connection_type === 'conditional')" in src
        # 2026-06-01: refactor do saveConnection para suportar dois eixos
        # ortogonais no mesmo `config` (expr + context_scope). A expr
        # continua sendo serializada exclusivamente quando type=conditional,
        # mas agora via `cfg.expr = expr` em vez do ternário inline antigo.
        assert "cfg.expr = expr" in src
        assert "JSON.stringify(cfg)" in src

    def test_topology_endpoint_returns_config(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "app" / "routes" / "mesh.py").read_text(encoding="utf-8")
        # Edge agora carrega config (string JSON)
        assert "\"config\": c.get(\"config\") or \"{}\"" in src
