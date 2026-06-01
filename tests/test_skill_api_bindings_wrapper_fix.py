"""Regressão do bug do Wizard "IA, me ajude" reprovar na validação.

User reportou (2026-05-31): SKILL.md gerado pelo botão "IA, me ajude" do
wizard de criação de Skill caía na validação com a mensagem
"execution_mode=declarative exige ## API Bindings OU ## Data Tables com
pelo menos 1 entrada válida" — apesar do bloco `## API Bindings` estar
presente com 1 endpoint.

Causa-raiz: dois desencontros entre Wizard e Validador:

1. Wizard emitia YAML com mapping no topo (`endpoints: [...]`), parser
   canônico só aceitava lista pura (`- id: ...`) — `yaml.safe_load`
   retornava dict, `isinstance(data, list)` era False, parser devolvia [].

2. Wizard emitia `# URL: ...` como comentário, mas o linter exige campo
   `path:` populado.

Adicionalmente, o linter exigia `connector` mas o wizard emitia
`connector_id` — segundo erro que o operador veria depois de corrigir
o primeiro.

Este arquivo tranca os contratos das 3 correções.
"""
from __future__ import annotations

import pytest

from app.skill_parser.parser import _parse_api_bindings, parse_skill_md
from app.skill_parser.linter import lint_skill


# ─── Parser: tolerância a dict com chave 'endpoints' / 'bindings' ────


class TestParserAcceptsEndpointsWrapper:
    def test_pure_list_still_accepted(self):
        """Formato canônico (lista direta) continua passando."""
        section = (
            "```yaml\n"
            "- id: ep-1\n"
            "  connector: c-1\n"
            "  name: Listar\n"
            "  method: GET\n"
            "  path: /v1/items\n"
            "```\n"
        )
        out = _parse_api_bindings(section)
        assert len(out) == 1
        assert out[0]["id"] == "ep-1"
        assert out[0]["path"] == "/v1/items"

    def test_endpoints_wrapper_accepted(self):
        """Formato do wizard antigo (`endpoints:` no topo) agora é aceito."""
        section = (
            "```yaml\n"
            "endpoints:\n"
            "  - id: ep-1\n"
            "    connector_id: c-1\n"
            "    name: Consultar CEP\n"
            "    method: GET\n"
            "    path: /api/cep/v1/{cep}\n"
            "```\n"
        )
        out = _parse_api_bindings(section)
        assert len(out) == 1, "Wrapper `endpoints:` deve ser desembrulhado"
        assert out[0]["id"] == "ep-1"
        assert out[0]["connector_id"] == "c-1"
        assert out[0]["path"] == "/api/cep/v1/{cep}"

    def test_bindings_wrapper_accepted(self):
        """Variação `bindings:` também é aceita (LLM oscila o nome)."""
        section = (
            "```yaml\n"
            "bindings:\n"
            "  - id: ep-9\n"
            "    connector: c-9\n"
            "    method: POST\n"
            "    path: /v1/things\n"
            "```\n"
        )
        out = _parse_api_bindings(section)
        assert len(out) == 1
        assert out[0]["id"] == "ep-9"

    def test_unknown_wrapper_returns_empty(self):
        """Mapping com chave estranha (sem endpoints/bindings) ainda dá []."""
        section = (
            "```yaml\n"
            "stuff:\n"
            "  - id: ep-1\n"
            "    path: /v1/x\n"
            "```\n"
        )
        out = _parse_api_bindings(section)
        assert out == []

    def test_yaml_comments_dont_block_parse(self):
        """Comentários YAML (`# foo`) são ignorados pelo safe_load."""
        section = (
            "```yaml\n"
            "endpoints:\n"
            "  - id: ep-1\n"
            "    connector_id: c-1\n"
            "    method: GET\n"
            "    # URL: https://example.com/v1/x\n"
            "    path: /v1/x\n"
            "```\n"
        )
        out = _parse_api_bindings(section)
        assert len(out) == 1


# ─── Integração: SKILL.md no formato do Wizard sobrevive ao parser ───


class TestEndToEndSkillFromWizardFormat:
    def test_wizard_style_skill_passes_validation(self):
        """SKILL.md com bloco no formato `endpoints:` do wizard antigo
        agora passa em parse_skill_md sem o erro de "exige ... entrada válida"."""
        md = (
            "---\n"
            "id: urn:skill:tech:subagent:cep\n"
            "version: 0.1.0\n"
            "kind: subagent\n"
            "execution_mode: declarative\n"
            "---\n"
            "\n"
            "# Consultar CEP\n"
            "\n"
            "## Purpose\n"
            "Recebe CEP, devolve endereço.\n"
            "\n"
            "## API Bindings\n"
            "```yaml\n"
            "endpoints:\n"
            "  - id: ep-cep\n"
            "    connector_id: c-brasilapi\n"
            "    name: Consultar CEP\n"
            "    method: GET\n"
            "    path: /api/cep/v1/{cep}\n"
            "```\n"
        )
        result = parse_skill_md(md)
        assert result.api_bindings_parsed, (
            "Parser ainda não desembrulhou `endpoints:`. Erros: "
            f"{result.validation_errors}"
        )
        assert result.api_bindings_parsed[0]["id"] == "ep-cep"
        # E o erro específico do bug não aparece mais
        for e in result.validation_errors:
            assert "exige ## API Bindings OU ## Data Tables" not in e


# ─── Linter: aceitar connector_id como alias + mensagem consistente ──


class _StubParsed:
    """Stub mínimo que imita ParsedSkill (linter usa só execution_mode +
    api_bindings_parsed)."""
    def __init__(self, execution_mode, bindings):
        self.execution_mode = execution_mode
        self.api_bindings_parsed = bindings


class TestLinterConnectorAlias:
    def _binding(self, **overrides):
        base = {
            "id": "ep-1",
            "method": "GET",
            "path": "/v1/x",
            "output_mapping": [{"from": "$.x", "to": "x"}],
        }
        base.update(overrides)
        return base

    def test_connector_field_passes(self):
        """`connector` (canônico) continua passando."""
        b = self._binding(connector="c-1")
        issues = lint_skill(_StubParsed("declarative", [b]))
        codes = [i["code"] for i in issues]
        assert "missing_connector" not in codes

    def test_connector_id_alias_also_passes(self):
        """`connector_id` (formato emitido pelo wizard) também serve."""
        b = self._binding(connector_id="c-1")
        issues = lint_skill(_StubParsed("declarative", [b]))
        codes = [i["code"] for i in issues]
        assert "missing_connector" not in codes, (
            f"`connector_id` deveria contar como conector válido. issues={issues}"
        )

    def test_neither_field_fails(self):
        """Sem connector nem connector_id continua sendo erro."""
        b = self._binding()
        issues = lint_skill(_StubParsed("declarative", [b]))
        codes = [i["code"] for i in issues]
        assert "missing_connector" in codes


class TestLinterParserMessageAlignment:
    def test_no_bindings_message_matches_parser(self):
        """Quando execution_mode=declarative e bindings está vazio, o linter
        agora usa a MESMA mensagem do parser (parser.py:202-204). Antes os
        dois caminhos divergiam — operador via "exige pelo menos 1 binding"
        num lugar e "exige ... OU ## Data Tables ..." no outro pra mesma causa."""
        issues = lint_skill(_StubParsed("declarative", []))
        msgs = [i["message"] for i in issues if i["code"] == "declarative_without_bindings"]
        assert msgs, f"linter não disparou o issue esperado: {issues}"
        assert "exige ## API Bindings OU ## Data Tables" in msgs[0]
