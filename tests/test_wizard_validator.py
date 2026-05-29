"""Testes do wizard_validator — validador pós-geração de SKILL.md.

Motivação: fechar o gap entre "regra no prompt do Wizard" (smoke estático
testado em test_wizard_skill.py) e "LLM gerador realmente seguiu a regra"
— gap pelo qual os bugs Context7 v1 (Workflow passivo) e v2 (operation
inventada) escaparam para runtime apesar dos prompt tests passando.

O validador é Python puro (sem LLM), determinístico, rodável em CI.
Vai ser plugado no endpoint /wizard/skill pra:
- Crítico: retry com instrução de correção
- Aviso: warning no response (frontend mostra antes de salvar)

Testes cobrem:
- Cada regra (G1.passive_verb, G1.no_imperative, G2.internal_phrase,
  G3.examples_without_tool_call, G4.negative_source, operation.invented)
  com SKILL boa (passa) e SKILL ruim (falha)
- Regressão direta dos casos reais Context7 v1 e v2
- Edge cases: sem binding (back-compat), operations não declaradas
- Helpers internos (_extract_operations_from_workflow, _has_imperative_verb)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.skill_parser.wizard_validator import (
    IMPERATIVE_VERBS,
    PASSIVE_VERBS,
    ValidationResult,
    Violation,
    _extract_operations_from_workflow,
    _has_imperative_verb,
    _find_passive_phrase,
    _find_internal_phrase,
    _find_negative_source_phrase,
    _examples_have_tool_call_marker,
    _split_operations_csv,
    validate_generated_skill,
)


# ───────────────────────────────────────────────────────────────
# Helpers de teste
# ───────────────────────────────────────────────────────────────


@dataclass
class _FakeSkill:
    """Mock mínimo de ParsedSkill — só os campos usados pelo validador."""
    workflow: str = ""
    examples: str = ""
    evidence_policy: str = ""
    tool_bindings: str = ""


def _mcp_bindings(name="Tool X", ops="docs,code"):
    return {
        "mcp_tools": [{"name": name, "operations": ops}],
        "rag_sources": [], "data_tables": [], "api_endpoints": [],
    }


def _no_bindings():
    return {
        "mcp_tools": [], "rag_sources": [],
        "data_tables": [], "api_endpoints": [],
    }


# ───────────────────────────────────────────────────────────────
# Helpers internos
# ───────────────────────────────────────────────────────────────


class TestExtractOperationsFromWorkflow:
    def test_extracts_operation_equals_value(self):
        wf = "Chame a tool com operation=docs e query=..."
        assert _extract_operations_from_workflow(wf) == ["docs"]

    def test_extracts_operation_with_backticks(self):
        wf = "Chame a tool com `operation=search` e query=..."
        assert _extract_operations_from_workflow(wf) == ["search"]

    def test_extracts_operation_with_quotes(self):
        wf = 'Chame com operation="code" e query=...'
        assert _extract_operations_from_workflow(wf) == ["code"]

    def test_extracts_multiple_operations(self):
        wf = "Passo 1: operation=docs. Passo 2: operation=code."
        ops = _extract_operations_from_workflow(wf)
        assert "docs" in ops and "code" in ops

    def test_handles_portuguese_operação(self):
        wf = "Use operação=docs para buscar"
        assert _extract_operations_from_workflow(wf) == ["docs"]

    def test_empty_workflow_returns_empty_list(self):
        assert _extract_operations_from_workflow("") == []

    def test_workflow_without_operation_returns_empty(self):
        assert _extract_operations_from_workflow("Texto solto sem nada") == []


class TestSplitOperationsCsv:
    def test_csv(self):
        assert _split_operations_csv("docs,code,prompt") == ["docs", "code", "prompt"]

    def test_csv_with_spaces(self):
        assert _split_operations_csv("docs, code , prompt") == ["docs", "code", "prompt"]

    def test_json_list(self):
        assert _split_operations_csv('["docs","code"]') == ["docs", "code"]

    def test_empty(self):
        assert _split_operations_csv("") == []
        assert _split_operations_csv("   ") == []


class TestHasImperativeVerb:
    @pytest.mark.parametrize("wf", [
        "Chame a tool Context 7 com operation=docs",
        "1. **Consulte** as bases RAG com query=...",
        "Execute o endpoint /cep com payload=...",
        "Acione a tool MCP",
        "Invoque o binding declarativo",
        "Query a tabela vendas com SELECT...",
    ])
    def test_detects_imperative_verbs(self, wf):
        assert _has_imperative_verb(wf) is True

    @pytest.mark.parametrize("wf", [
        "Enriquecimento com Context 7 usando o binding",
        "A partir de informações do recurso interno",
        "Mapeamento de pattern_type pra template",
        "Geração de resposta",
    ])
    def test_rejects_passive_phrases(self, wf):
        assert _has_imperative_verb(wf) is False


class TestFindPassivePhrase:
    def test_detects_enriquecimento(self):
        assert _find_passive_phrase("Enriquecimento com Context 7") == "enriquecimento"

    def test_detects_usando_o_binding(self):
        assert _find_passive_phrase("incorpora info usando o binding X") == "incorpora"

    def test_none_when_clean(self):
        assert _find_passive_phrase("Chame a tool com operation=docs") is None


class TestFindInternalPhrase:
    def test_detects_template_interno(self):
        assert _find_internal_phrase("seleciona o template interno") == "template interno"

    def test_detects_conhecimento_proprio(self):
        assert _find_internal_phrase("usa conhecimento próprio") == "conhecimento próprio"

    def test_none_when_clean(self):
        assert _find_internal_phrase("consulta a base RAG") is None


class TestFindNegativeSourcePhrase:
    def test_detects_nenhuma_fonte_externa(self):
        text = "Nenhuma fonte externa autorizada para este skill"
        assert _find_negative_source_phrase(text) is not None

    def test_detects_normalized_whitespace(self):
        """Detecção precisa ser robusta a quebras de linha + múltiplos espaços."""
        text = "Nenhuma\n  fonte  externa\nautorizada"
        assert _find_negative_source_phrase(text) is not None


class TestExamplesHaveToolCallMarker:
    def test_detects_chamada_a_tool(self):
        ex = "**Chamada à tool:** `X` operation=docs"
        assert _examples_have_tool_call_marker(ex) is True

    def test_detects_resposta_da_tool(self):
        ex = "**Resposta da tool (resumo):** ..."
        assert _examples_have_tool_call_marker(ex) is True

    def test_detects_sql_gerado(self):
        ex = "**SQL gerado:** SELECT * FROM vendas"
        assert _examples_have_tool_call_marker(ex) is True

    def test_rejects_only_output(self):
        ex = "Entrada: x\nSaída: {pattern: y}"
        assert _examples_have_tool_call_marker(ex) is False

    def test_empty_returns_false(self):
        assert _examples_have_tool_call_marker("") is False


# ───────────────────────────────────────────────────────────────
# Validador principal — regra por regra
# ───────────────────────────────────────────────────────────────


class TestG1PassiveVerb:
    def test_skill_with_imperative_workflow_passes(self):
        skill = _FakeSkill(workflow="Chame a tool com operation=docs")
        result = validate_generated_skill(skill, _mcp_bindings())
        assert result.ok
        assert not any(v.rule == "G1.passive_verb" for v in result.violations)

    def test_skill_with_enriquecimento_fails_critical(self):
        skill = _FakeSkill(workflow="Enriquecimento com Tool X usando o binding")
        result = validate_generated_skill(skill, _mcp_bindings())
        assert not result.ok
        passive = [v for v in result.violations if v.rule == "G1.passive_verb"]
        assert len(passive) == 1
        assert passive[0].severity == "critical"
        assert "enriquecimento" in passive[0].message.lower()
        # Suggestion precisa ter verbo imperativo concreto
        assert any(verb in passive[0].suggestion for verb in ("Chame", "Consulte", "Execute"))

    def test_rule_skipped_when_no_bindings(self):
        """Skill sem nenhum binding — workflow com 'enriquecimento' é OK
        porque não há tool pra ignorar."""
        skill = _FakeSkill(workflow="Geração com enriquecimento de prompt")
        result = validate_generated_skill(skill, _no_bindings())
        assert not any(v.rule == "G1.passive_verb" for v in result.violations)


class TestG1NoImperativeVerb:
    def test_workflow_with_imperative_passes(self):
        skill = _FakeSkill(workflow="1. Chame a tool com operation=docs")
        result = validate_generated_skill(skill, _mcp_bindings())
        assert not any(v.rule == "G1.no_imperative" for v in result.violations)

    def test_workflow_descritivo_sem_verbo_fails(self):
        skill = _FakeSkill(workflow="Mapeamento de pattern_type pra template. Geração de resposta.")
        result = validate_generated_skill(skill, _mcp_bindings())
        no_imp = [v for v in result.violations if v.rule == "G1.no_imperative"]
        assert len(no_imp) == 1
        assert no_imp[0].severity == "critical"

    def test_rule_skipped_when_no_bindings(self):
        """Skill puramente de raciocínio sem binding — não exige verbo."""
        skill = _FakeSkill(workflow="Análise e geração de resposta.")
        result = validate_generated_skill(skill, _no_bindings())
        assert not any(v.rule == "G1.no_imperative" for v in result.violations)


class TestG2InternalPhrase:
    def test_template_interno_detected(self):
        skill = _FakeSkill(workflow="Chame a tool. Selecione o template interno correspondente.")
        result = validate_generated_skill(skill, _mcp_bindings())
        crits = [v for v in result.violations if v.rule == "G2.internal_phrase"]
        assert len(crits) == 1
        assert "template interno" in crits[0].message.lower()

    def test_conhecimento_proprio_detected(self):
        skill = _FakeSkill(workflow="Chame a tool e use conhecimento próprio pra completar.")
        result = validate_generated_skill(skill, _mcp_bindings())
        assert any(v.rule == "G2.internal_phrase" for v in result.violations)

    def test_clean_workflow_passes(self):
        skill = _FakeSkill(workflow="Chame a tool com operation=docs e query=...")
        result = validate_generated_skill(skill, _mcp_bindings())
        assert not any(v.rule == "G2.internal_phrase" for v in result.violations)


class TestG3ExamplesWithoutToolCall:
    def test_examples_with_tool_call_marker_passes(self):
        ex = (
            "### Exemplo 1\n"
            "**Entrada:** ...\n"
            "**Chamada à tool:** `X` operation=docs\n"
            "**Resposta da tool:** ...\n"
            "**Saída final:** ..."
        )
        skill = _FakeSkill(
            workflow="Chame a tool com operation=docs",
            examples=ex,
        )
        result = validate_generated_skill(skill, _mcp_bindings())
        assert not any(v.rule == "G3.examples_without_tool_call" for v in result.violations)

    def test_examples_without_tool_call_warns(self):
        ex = "Entrada: x\nSaída: {pattern: 'mvc'}"
        skill = _FakeSkill(
            workflow="Chame a tool com operation=docs",
            examples=ex,
        )
        result = validate_generated_skill(skill, _mcp_bindings())
        warns = [v for v in result.violations if v.rule == "G3.examples_without_tool_call"]
        assert len(warns) == 1
        assert warns[0].severity == "warning"
        # G3 é warning — não bloqueia (ok pode ser True)
        assert result.ok is True

    def test_empty_examples_does_not_warn(self):
        """Skill sem ## Examples não dispara o aviso."""
        skill = _FakeSkill(
            workflow="Chame a tool com operation=docs",
            examples="",
        )
        result = validate_generated_skill(skill, _mcp_bindings())
        assert not any(v.rule == "G3.examples_without_tool_call" for v in result.violations)


class TestG4NegativeSource:
    def test_evidence_policy_with_nenhuma_fonte_externa_fails(self):
        skill = _FakeSkill(
            workflow="Chame a tool com operation=docs",
            evidence_policy="Nenhuma fonte externa autorizada para este skill.",
        )
        result = validate_generated_skill(skill, _mcp_bindings())
        crits = [v for v in result.violations if v.rule == "G4.negative_source"]
        assert len(crits) == 1
        assert crits[0].section == "Evidence Policy"

    def test_workflow_with_negative_source_phrase_fails(self):
        skill = _FakeSkill(
            workflow="Chame a tool. Sem fontes externas autorizadas.",
        )
        result = validate_generated_skill(skill, _mcp_bindings())
        assert any(v.rule == "G4.negative_source" for v in result.violations)

    def test_clean_evidence_policy_passes(self):
        skill = _FakeSkill(
            workflow="Chame a tool com operation=docs",
            evidence_policy="A única fonte autorizada é o binding Tool X.",
        )
        result = validate_generated_skill(skill, _mcp_bindings())
        assert not any(v.rule == "G4.negative_source" for v in result.violations)


class TestOperationInvented:
    """Bug Context7 v2: SKILL pediu operation=search mas Registry só tinha
    docs/code/prompt. Validador precisa flagar."""

    def test_workflow_with_search_when_only_docs_declared_fails(self):
        skill = _FakeSkill(workflow="Chame a tool com operation=search e query=...")
        bindings = _mcp_bindings(name="Context 7 MCP Server", ops="docs,code,prompt")
        result = validate_generated_skill(skill, bindings)
        crits = [v for v in result.violations if v.rule == "operation.invented"]
        assert len(crits) == 1
        assert crits[0].severity == "critical"
        assert "search" in crits[0].message.lower()
        # Suggestion deve listar as operations válidas
        assert "docs" in crits[0].suggestion

    def test_workflow_with_declared_operation_passes(self):
        skill = _FakeSkill(workflow="Chame a tool com operation=docs e query=...")
        bindings = _mcp_bindings(name="Context 7", ops="docs,code,prompt")
        result = validate_generated_skill(skill, bindings)
        assert not any(v.rule == "operation.invented" for v in result.violations)

    def test_rule_skipped_when_tool_has_no_operations_declared(self):
        """Tool nova sem operations no Registry — validador não pode dizer
        que `search` é inválida (não sabe quais são as válidas)."""
        skill = _FakeSkill(workflow="Chame a tool com operation=search")
        bindings = _mcp_bindings(name="Mystery", ops="")
        result = validate_generated_skill(skill, bindings)
        assert not any(v.rule == "operation.invented" for v in result.violations)

    def test_multiple_invented_operations_listed(self):
        skill = _FakeSkill(
            workflow=(
                "Passo 1: operation=search e query=...\n"
                "Passo 2: operation=fetch e query=..."
            )
        )
        bindings = _mcp_bindings(ops="docs,code")
        result = validate_generated_skill(skill, bindings)
        crits = [v for v in result.violations if v.rule == "operation.invented"]
        assert len(crits) == 1
        # Lista as duas inventadas na mensagem
        assert "search" in crits[0].message and "fetch" in crits[0].message

    def test_case_insensitive_match(self):
        """operation=DOCS é equivalente a operation=docs."""
        skill = _FakeSkill(workflow="operation=DOCS")
        bindings = _mcp_bindings(ops="docs,code")
        result = validate_generated_skill(skill, bindings)
        assert not any(v.rule == "operation.invented" for v in result.violations)

    def test_validator_handles_json_operations_list(self):
        """Registry pode armazenar operations como JSON list em vez de CSV."""
        skill = _FakeSkill(workflow="operation=docs")
        bindings = {
            "mcp_tools": [{"name": "X", "operations": '["docs", "code"]'}],
            "rag_sources": [], "data_tables": [], "api_endpoints": [],
        }
        result = validate_generated_skill(skill, bindings)
        assert not any(v.rule == "operation.invented" for v in result.violations)


# ───────────────────────────────────────────────────────────────
# Regressão dos bugs reais Context7 v1 e v2
# ───────────────────────────────────────────────────────────────


class TestOperationMissing:
    """Bug Context7 v3 (2026-05-29 #3): Workflow não cita NENHUMA operation
    mesmo com Registry declarando docs/code/prompt. Engine em runtime não
    sabe qual operation invocar — tool nunca é chamada.

    Diferente do operation.invented (que pega 'search' quando Registry tem
    docs/code/prompt), aqui o Workflow não cita NADA — omissão silenciosa
    que escapa do validador v2.
    """

    def test_workflow_without_operation_when_registry_declares_fails(self):
        skill = _FakeSkill(
            workflow="1. Chame a tool Context 7 MCP Server para obter docs do pattern.",
        )
        bindings = _mcp_bindings(name="Context 7 MCP Server", ops="docs,code,prompt")
        result = validate_generated_skill(skill, bindings)
        crits = [v for v in result.violations if v.rule == "operation.missing"]
        assert len(crits) == 1
        assert crits[0].severity == "critical"
        assert "docs" in crits[0].message and "code" in crits[0].message

    def test_workflow_with_operation_passes(self):
        skill = _FakeSkill(workflow="Chame a tool com operation=docs e query=...")
        bindings = _mcp_bindings(name="Context 7", ops="docs,code,prompt")
        result = validate_generated_skill(skill, bindings)
        assert not any(v.rule == "operation.missing" for v in result.violations)

    def test_skip_when_no_operations_declared_in_registry(self):
        """Tool sem operations no Registry — não dá pra dizer que está
        'missing', porque não há operations a citar."""
        skill = _FakeSkill(workflow="Chame a tool.")
        bindings = _mcp_bindings(name="Mystery", ops="")
        result = validate_generated_skill(skill, bindings)
        assert not any(v.rule == "operation.missing" for v in result.violations)

    def test_suggestion_lists_valid_operations(self):
        skill = _FakeSkill(workflow="Chame a tool para obter info.")
        bindings = _mcp_bindings(ops="docs,code,prompt")
        result = validate_generated_skill(skill, bindings)
        crit = next(v for v in result.violations if v.rule == "operation.missing")
        for op in ("docs", "code", "prompt"):
            assert op in crit.suggestion

    def test_operation_invented_takes_precedence_over_missing(self):
        """Quando há operation citada (mesmo inventada), dispara
        operation.invented em vez de operation.missing — evita ruído duplo."""
        skill = _FakeSkill(workflow="Chame a tool com operation=search.")
        bindings = _mcp_bindings(ops="docs,code")
        result = validate_generated_skill(skill, bindings)
        rules = {v.rule for v in result.violations}
        assert "operation.invented" in rules
        assert "operation.missing" not in rules


class TestOperationContradictsRegistry:
    """Bug Context7 v3 #2: Tool Bindings escreve texto inventado como
    'nenhuma operação declarada' / 'operações não disponíveis' mesmo
    quando o Registry declara docs/code/prompt.

    Esse texto enviesa o LLM em runtime a NÃO chamar a tool, achando que
    ela não tem operations utilizáveis.
    """

    @pytest.mark.parametrize("phrase", [
        "nenhuma operação declarada",
        "Nenhuma operação declarada para esta tool",
        "operações não declaradas",
        "Sem operações disponíveis",
        "operações não disponíveis para uso",
    ])
    def test_detects_various_contradiction_phrases(self, phrase):
        skill = _FakeSkill(
            workflow="Chame a tool com operation=docs.",
            tool_bindings=f"- Context 7 MCP Server\n  *Operações*: {phrase}",
        )
        bindings = _mcp_bindings(ops="docs,code,prompt")
        result = validate_generated_skill(skill, bindings)
        crits = [v for v in result.violations if v.rule == "operation.contradicts_registry"]
        assert len(crits) == 1

    def test_clean_tool_bindings_passes(self):
        skill = _FakeSkill(
            workflow="Chame a tool com operation=docs.",
            tool_bindings="- Context 7\n  Operations: docs, code, prompt",
        )
        bindings = _mcp_bindings(ops="docs,code,prompt")
        result = validate_generated_skill(skill, bindings)
        assert not any(v.rule == "operation.contradicts_registry" for v in result.violations)

    def test_no_contradiction_when_no_ops_in_registry(self):
        """Quando o Registry NÃO tem operations, dizer 'sem operações' é
        verdade — não dispara."""
        skill = _FakeSkill(
            workflow="Chame a tool.",
            tool_bindings="- Mystery Tool\n  *Operações*: nenhuma operação declarada",
        )
        bindings = _mcp_bindings(ops="")
        result = validate_generated_skill(skill, bindings)
        assert not any(v.rule == "operation.contradicts_registry" for v in result.violations)

    def test_suggestion_lists_actual_operations_from_registry(self):
        skill = _FakeSkill(
            workflow="Chame a tool com operation=docs.",
            tool_bindings="*Operações*: nenhuma operação declarada",
        )
        bindings = _mcp_bindings(ops="docs,code,prompt")
        result = validate_generated_skill(skill, bindings)
        crit = next(v for v in result.violations if v.rule == "operation.contradicts_registry")
        for op in ("docs", "code", "prompt"):
            assert op in crit.suggestion


class TestRegressionContext7V1:
    """SKILL gerada (anterior a PR #180) tinha Workflow passivo +
    Evidence Policy contraditória. Validador agora pega ambos."""

    SKILL_RUIM_V1 = _FakeSkill(
        workflow=(
            "1. Validação de entrada.\n"
            "2. Mapeamento de padrão — seleciona o template interno.\n"
            "3. Enriquecimento com Context 7 — incorpora informações usando o binding.\n"
            "4. Geração de artefato."
        ),
        evidence_policy=(
            "Nenhuma fonte de conhecimento externa está autorizada para este skill. "
            "Toda informação utilizada provém do binding Context 7 MCP Server."
        ),
    )

    def test_v1_skill_fails_multiple_critical_rules(self):
        result = validate_generated_skill(
            self.SKILL_RUIM_V1,
            _mcp_bindings(name="Context 7 MCP Server", ops="docs,code,prompt"),
        )
        assert not result.ok
        assert result.critical_count >= 3  # passive + internal + negative_source
        rules_hit = {v.rule for v in result.violations}
        assert "G1.passive_verb" in rules_hit
        assert "G2.internal_phrase" in rules_hit
        assert "G4.negative_source" in rules_hit

    def test_v1_critical_suggestions_contain_actionable_fixes(self):
        result = validate_generated_skill(
            self.SKILL_RUIM_V1,
            _mcp_bindings(name="Context 7 MCP Server", ops="docs,code,prompt"),
        )
        suggestions = result.critical_suggestions()
        assert len(suggestions) >= 3
        # Pelo menos uma sugestão menciona verbo imperativo concreto
        joined = " ".join(suggestions)
        assert "Chame" in joined or "Consulte" in joined


class TestRegressionContext7V2:
    """SKILL gerada após PR #180 (Workflow imperativo OK) mas com
    operation=search inventada. Causou erro em runtime."""

    SKILL_RUIM_V2 = _FakeSkill(
        workflow=(
            "1. **Chame** a tool `Context 7 MCP Server` com "
            "`operation=search` e `query=<design_pattern_request>` "
            "**antes** de gerar a resposta.\n"
            "2. Avalie a relevância da resposta."
        ),
    )

    def test_v2_skill_fails_only_operation_invented(self):
        """V2 tem Workflow imperativo correto (G1 OK), sem template interno
        (G2 OK), sem frase negativa (G4 OK). A única falha é operation."""
        result = validate_generated_skill(
            self.SKILL_RUIM_V2,
            _mcp_bindings(name="Context 7 MCP Server", ops="docs,code,prompt"),
        )
        assert not result.ok
        rules_hit = {v.rule for v in result.violations}
        assert "operation.invented" in rules_hit
        # G1/G2/G4 NÃO devem disparar (v2 fix já estava parcial)
        assert "G1.passive_verb" not in rules_hit
        assert "G1.no_imperative" not in rules_hit
        assert "G2.internal_phrase" not in rules_hit
        assert "G4.negative_source" not in rules_hit

    def test_v2_suggestion_lists_valid_operations(self):
        result = validate_generated_skill(
            self.SKILL_RUIM_V2,
            _mcp_bindings(name="Context 7 MCP Server", ops="docs,code,prompt"),
        )
        crit = next(v for v in result.violations if v.rule == "operation.invented")
        # As 3 operations válidas estão na sugestão
        for op in ("docs", "code", "prompt"):
            assert op in crit.suggestion


class TestRegressionRealContext7Skill:
    """Regressão usando SKILL.md REAL que o user colou em 2026-05-29
    (Context7 Design Pattern Generator v0.1.0, gerada antes de PR #185).

    Diferente da TestRegressionContext7V2 que usa SKILL fictícia simplificada,
    aqui rodamos parse_skill_md sobre o arquivo .md completo (com YAML
    frontmatter, todas as 11 seções, examples reais) — garante que o
    validador funciona ponta-a-ponta sobre output real do Wizard.
    """

    FIXTURE = Path("tests/fixtures/context7_skill_buggy.md")

    def _bindings(self):
        return {
            "mcp_tools": [{
                "id": "481c5fa3-36bc-4d05-97ff-d502d93521ff",
                "name": "Context 7 MCP Server",
                "description": "Plataforma Context7 para documentação atualizada",
                "operations": "docs,code,prompt",
            }],
            "rag_sources": [],
            "data_tables": [],
            "api_endpoints": [],
        }

    def test_fixture_exists_and_has_buggy_operation(self):
        """Sanity: a fixture realmente contém operation=search."""
        assert self.FIXTURE.exists(), "fixture context7_skill_buggy.md ausente"
        content = self.FIXTURE.read_text(encoding="utf-8")
        assert "operation=search" in content
        # E pelo menos 2 vezes (Workflow + Examples) — bug no fluxo todo
        assert content.count("operation=search") >= 2

    def test_real_context7_skill_fails_validation(self):
        """Roda parser + validador na SKILL real do user. Deve detectar
        exatamente 1 crítico (operation.invented) e 0 warning."""
        from app.skill_parser.parser import parse_skill_md
        skill_md = self.FIXTURE.read_text(encoding="utf-8")
        parsed = parse_skill_md(skill_md)
        result = validate_generated_skill(parsed, self._bindings())

        assert not result.ok
        assert result.critical_count == 1
        rules = {v.rule for v in result.violations}
        assert rules == {"operation.invented"}

    def test_real_context7_violation_cites_search(self):
        """A violation precisa identificar exatamente 'search' como inventada
        e listar docs/code/prompt como válidas — pra retry instruction ser útil."""
        from app.skill_parser.parser import parse_skill_md
        parsed = parse_skill_md(self.FIXTURE.read_text(encoding="utf-8"))
        result = validate_generated_skill(parsed, self._bindings())

        crit = result.violations[0]
        assert "search" in crit.message
        assert "code" in crit.suggestion
        assert "docs" in crit.suggestion
        assert "prompt" in crit.suggestion

    def test_real_context7_other_rules_pass(self):
        """A SKILL real tem Workflow imperativo ('Chame'), Evidence Policy
        correta ('única fonte autorizada'), Examples com rastreabilidade.
        Validador NÃO deve flagar G1/G2/G4."""
        from app.skill_parser.parser import parse_skill_md
        parsed = parse_skill_md(self.FIXTURE.read_text(encoding="utf-8"))
        result = validate_generated_skill(parsed, self._bindings())

        rules = {v.rule for v in result.violations}
        # Os PRs #180+#181+#185 corrigiram esses — não devem aparecer
        assert "G1.passive_verb" not in rules
        assert "G1.no_imperative" not in rules
        assert "G2.internal_phrase" not in rules
        assert "G4.negative_source" not in rules


class TestRegressionContext7DocumentFetcher:
    """Regressão do 3º bug Context7 reportado (2026-05-29 #4): user gerou
    nova SKILL 'Context7 Documentation Fetcher' (urn:...:context7-document-
    fetch) sem operation no Workflow. Engine forçou operation arbitrária
    via enum required do MCP function spec, Context7 retornou nada útil,
    LLM tentou preencher Output Contract estrito → resposta VAZIA (bolha
    em branco no chat).

    Causa raiz: SKILL gerada ANTES do PR #186 mergear (validador no
    endpoint), então omitiu operation sem ninguém pegar.

    Esta regressão garante que se o LLM gerador errar igual de novo, o
    validador atual (#188) pega via operation.missing.
    """

    FIXTURE = Path("tests/fixtures/context7_skill_document_fetcher.md")

    def _bindings(self):
        return {
            "mcp_tools": [{
                "id": "481c5fa3-36bc-4d05-97ff-d502d93521ff",
                "name": "Context 7 MCP Server",
                "description": "Plataforma Context7 para documentação atualizada",
                "operations": "docs,code,prompt",
            }],
            "rag_sources": [],
            "data_tables": [],
            "api_endpoints": [],
        }

    def test_fixture_exists_and_workflow_has_no_operation(self):
        assert self.FIXTURE.exists()
        content = self.FIXTURE.read_text(encoding="utf-8")
        # Sanity: Workflow tem 'query=' mas NÃO tem 'operation=' em parte alguma
        # (essa é a essência do bug)
        assert "query=" in content
        # Procura apenas dentro da seção Workflow — Examples podem ter
        # operation no marcador "Chamada à tool" mas Workflow não tem.
        import re as _re
        m = _re.search(r"## Workflow\s*\n([\s\S]*?)\n## ", content)
        assert m
        workflow_section = m.group(1)
        assert "operation=" not in workflow_section, (
            "fixture deveria ter Workflow SEM operation — esse é o bug"
        )

    def test_fetcher_skill_fails_with_operation_missing(self):
        """Roda parse + validate na SKILL real. Deve detectar exatamente
        operation.missing como crítico."""
        from app.skill_parser.parser import parse_skill_md
        skill_md = self.FIXTURE.read_text(encoding="utf-8")
        parsed = parse_skill_md(skill_md)
        result = validate_generated_skill(parsed, self._bindings())

        assert not result.ok
        # operation.missing é a regra esperada
        rules = {v.rule for v in result.violations}
        assert "operation.missing" in rules
        # G1/G2/G4 NÃO devem disparar (Workflow imperativo + Evidence Policy
        # correta + Examples com rastreabilidade — esses funcionaram)
        assert "G1.passive_verb" not in rules
        assert "G1.no_imperative" not in rules
        assert "G2.internal_phrase" not in rules
        assert "G4.negative_source" not in rules

    def test_fetcher_suggestion_lists_valid_operations(self):
        """Suggestion deve listar docs/code/prompt como opções pra retry."""
        from app.skill_parser.parser import parse_skill_md
        parsed = parse_skill_md(self.FIXTURE.read_text(encoding="utf-8"))
        result = validate_generated_skill(parsed, self._bindings())

        crit = next(v for v in result.violations if v.rule == "operation.missing")
        for op in ("docs", "code", "prompt"):
            assert op in crit.suggestion

    def test_fetcher_explains_runtime_consequence(self):
        """Mensagem precisa conectar a omissão com a consequência real
        observada pelo user (tool não é chamada ou retorna vazia)."""
        from app.skill_parser.parser import parse_skill_md
        parsed = parse_skill_md(self.FIXTURE.read_text(encoding="utf-8"))
        result = validate_generated_skill(parsed, self._bindings())

        crit = next(v for v in result.violations if v.rule == "operation.missing")
        low = crit.message.lower()
        # Cita o sintoma: engine não saberá / tool não será chamada
        assert "engine" in low and ("não sabe" in low or "tool não será" in low)


# ───────────────────────────────────────────────────────────────
# ValidationResult shape
# ───────────────────────────────────────────────────────────────


class TestValidationResultShape:
    def test_empty_skill_passes_when_no_bindings(self):
        """Back-compat: skill puramente de raciocínio sem nada — passa."""
        skill = _FakeSkill()
        result = validate_generated_skill(skill, _no_bindings())
        assert result.ok
        assert result.critical_count == 0
        assert result.warning_count == 0

    def test_to_dict_serializable_for_api_response(self):
        skill = _FakeSkill(workflow="Enriquecimento com Tool X")
        result = validate_generated_skill(skill, _mcp_bindings())
        d = result.to_dict()
        assert "ok" in d and "violations" in d
        assert d["critical_count"] >= 1
        # Violations serialized como dicts
        assert isinstance(d["violations"], list)
        assert all(isinstance(v, dict) for v in d["violations"])
        # Cada violação tem os campos esperados
        for v in d["violations"]:
            assert {"rule", "severity", "section", "message", "suggestion"} <= set(v.keys())

    def test_critical_suggestions_capped_at_5(self):
        """Evita inflar o retry prompt com 20 sugestões."""
        skill = _FakeSkill(
            workflow=(
                "Enriquecimento com X usando o binding. "
                "Selecione template interno com conhecimento próprio. "
                "operation=search e operation=fetch e operation=query."
            ),
            evidence_policy="Nenhuma fonte externa autorizada.",
        )
        result = validate_generated_skill(skill, _mcp_bindings(ops="docs"))
        # Mesmo com várias violações, suggestions sai capped
        assert len(result.critical_suggestions()) <= 5
