"""Regressão do bug do fence_close ancorado em $ no parser de SKILL.md.

User reportou (2026-06-01): SKILL.md gerado pelo wizard com bloco
`## API Bindings` no formato canônico (lista pura, path:, connector +
connector_id) AINDA reprovava com "execution_mode=declarative exige ##
API Bindings OU ## Data Tables com pelo menos 1 entrada válida".

Causa-raiz (workflow diagnosticou): `_parse_api_bindings` removia o fence
de fechamento via `re.sub(r"\\n```\\s*$", "", body)` — só funcionava se
o ``` estivesse no final absoluto do `section_text`. Como o splitter
`_extract_sections` inclui tudo até o próximo `## `, sempre havia
conteúdo após o fence (no mínimo o HR `\\n\\n---\\n\\n` que o wizard
injeta entre seções obrigatórias). O ``` literal sobrava no body,
`yaml.safe_load` levantava `ScannerError`, `except yaml.YAMLError`
engolia, devolvia [].

Fix: helper `_extract_fenced_yaml_body` usa `rest.find("\\n```")` para
pegar o fim do fence onde quer que ele esteja. Aplicado a
`_parse_api_bindings` e `_parse_data_tables` (mesmo padrão).
"""
from __future__ import annotations


from app.skill_parser.parser import (
    _extract_fenced_yaml_body,
    _parse_api_bindings,
    _parse_data_tables,
    _parse_evidence_policy,
    _parse_output_shape,
    parse_skill_md,
)


# ─── _extract_fenced_yaml_body: o helper isolado ────────────────────


class TestExtractFencedYamlBody:
    def test_returns_body_between_fences(self):
        section = "intro\n```yaml\nkey: value\n```\nrest"
        assert _extract_fenced_yaml_body(section) == "key: value"

    def test_strips_content_after_closing_fence(self):
        """Bug original — o HR markdown `---` entre seções ficava no body."""
        section = (
            "\n```yaml\n"
            "- id: ep-1\n"
            "  path: /v1/x\n"
            "```\n"
            "\n---\n"
            "\n## Próxima seção"
        )
        body = _extract_fenced_yaml_body(section)
        assert "```" not in body, f"fence literal sobrou no body: {body!r}"
        assert "---" not in body, f"HR sobrou no body: {body!r}"
        assert "## Próxima" not in body

    def test_no_fence_returns_section_text_intact(self):
        """Modo inline (sem fence) — devolve o texto cru."""
        section = "- id: x\n  path: /v1/y"
        assert _extract_fenced_yaml_body(section) == section

    def test_unclosed_fence_returns_everything_after_open(self):
        """Fence aberto sem fechamento: degrade gracioso — pega o resto."""
        section = "```yaml\nkey: value\n"
        out = _extract_fenced_yaml_body(section)
        assert "key: value" in out

    def test_fence_at_absolute_end_still_works(self):
        """Caso happy-path original (fence no final absoluto) preservado."""
        section = "```yaml\n- id: x\n```"
        out = _extract_fenced_yaml_body(section)
        assert out.strip() == "- id: x"

    def test_accepts_yml_alias(self):
        """Fence pode ser ```yml em vez de ```yaml."""
        section = "```yml\na: 1\n```\nlixo"
        assert _extract_fenced_yaml_body(section) == "a: 1"


# ─── _parse_api_bindings: bug original reproduzido + fix ──────────


class TestApiBindingsWithTrailingContent:
    def test_section_with_trailing_hr_is_parsed(self):
        """REGRESSÃO do bug 2026-06-01: HR markdown depois do fence
        derrubava o parse silenciosamente."""
        section = (
            "\n```yaml\n"
            "  - id: 1ad339ec-ae07-41c0-bd6b-9874bd7de010\n"
            "    connector: bb48804e-0e73-4de7-9af0-1244b47bc9f6\n"
            "    connector_id: bb48804e-0e73-4de7-9af0-1244b47bc9f6\n"
            "    name: Consultar CEP\n"
            "    method: GET\n"
            "    path: /api/cep/v1/{cep}\n"
            "```\n"
            "\n---\n"
        )
        out = _parse_api_bindings(section)
        assert len(out) == 1, f"binding sumiu por causa do HR. out={out!r}"
        assert out[0]["id"] == "1ad339ec-ae07-41c0-bd6b-9874bd7de010"
        assert out[0]["path"] == "/api/cep/v1/{cep}"

    def test_section_with_trailing_text_is_parsed(self):
        """Variação: texto livre após o fence (nota, comentário, etc.)."""
        section = (
            "```yaml\n"
            "- id: ep-1\n"
            "  connector: c-1\n"
            "  path: /v1/x\n"
            "```\n"
            "\n(nota: este endpoint exige autenticação)\n"
        )
        out = _parse_api_bindings(section)
        assert len(out) == 1
        assert out[0]["id"] == "ep-1"

    def test_canonical_fence_no_trailing_still_works(self):
        """Compat: fence no final absoluto continua funcionando."""
        section = "```yaml\n- id: ep-1\n  path: /v1/x\n```"
        out = _parse_api_bindings(section)
        assert len(out) == 1


# ─── _parse_data_tables: mesmo bug, mesmo fix ──────────────────────


class TestDataTablesWithTrailingContent:
    def test_data_tables_with_trailing_hr_is_parsed(self):
        """`_parse_data_tables` tinha o mesmo bug do fence_close em $."""
        section = (
            "```yaml\n"
            "tables:\n"
            "  - id: vendas_q4\n"
            "    table_ref: urn:table:abcd1234:vendas-q4:1\n"
            "```\n"
            "\n---\n"
        )
        out = _parse_data_tables(section)
        assert len(out) == 1, f"data table sumiu por causa do HR. out={out!r}"
        assert out[0]["id"] == "vendas_q4"
        assert out[0]["table_ref"] == "urn:table:abcd1234:vendas-q4:1"


# ─── _parse_evidence_policy: MESMO bug (achado no teste E2E do Cenário A) ──
# Este parser ficou de FORA da migração de 2026-06-01 (só api_bindings e
# data_tables foram migrados). Consequência real (2026-07-08): TODA skill do
# wizard "IA, me ajude" com Fontes RAG tinha o `sources` descartado em silêncio
# → `_declares_sources=False` → especialista PULAVA o RAG em pipeline
# (evidence_count=0) → não usava o KB. Ver engine._pipeline_should_self_retrieve.


class TestEvidencePolicyWithTrailingContent:
    def test_evidence_sources_with_trailing_hr_is_parsed(self):
        """Formato EXATO gerado pelo wizard: fence + HR `---` antes da próxima
        seção. Antes: `sources` sumia (ScannerError silencioso)."""
        section = (
            "```yaml\n"
            "sources:\n"
            "  - ee1b4458-5237-433c-b099-8ba3dc3737bd   # Acolhimento (internal)\n"
            "```\n"
            "\n---\n\n"
        )
        out = _parse_evidence_policy(section)
        assert out.get("sources") == ["ee1b4458-5237-433c-b099-8ba3dc3737bd"], (
            f"sources sumiu por causa do HR/fence. out={out!r}"
        )

    def test_evidence_min_relevance_survives_trailing_content(self):
        section = (
            "```yaml\n"
            "sources:\n  - ks-1\n"
            "min_relevance: 0.25\n"
            "```\n\n---\n"
        )
        out = _parse_evidence_policy(section)
        assert out.get("sources") == ["ks-1"]
        assert out.get("min_relevance") == 0.25

    def test_evidence_no_fence_stays_legacy_raw(self):
        """Sem fence → legacy {raw} (sem governance) preservado."""
        out = _parse_evidence_policy("Consulte as bases internas antes de responder.")
        assert "sources" not in out
        assert out.get("raw")

    def test_evidence_fence_at_absolute_end_still_works(self):
        section = "```yaml\nsources:\n  - ks-9\n```"
        assert _parse_evidence_policy(section).get("sources") == ["ks-9"]


# ─── _parse_output_shape: mesmo bug, mesma migração ──────────────────


class TestOutputShapeWithTrailingContent:
    def test_length_preset_with_trailing_hr_is_parsed(self):
        section = "```yaml\nlength_preset: digest\n```\n\n---\n\n"
        out = _parse_output_shape(section)
        assert out.get("length_preset") == "digest", f"preset sumiu. out={out!r}"
        assert out.get("max_chars")


# ─── E2E: SKILL.md exato como o user reportou ──────────────────────


class TestEndToEndSkillFromUserReport:
    def test_skill_with_hr_between_sections_is_valid(self):
        """SKILL.md como o que o user mostrou na dúvida 2026-06-01:
        HR markdown entre `## Tool Bindings` e `## API Bindings`, e entre
        `## API Bindings` e `## Execution Profile`. Hoje passava em [].
        Com o fix: parser extrai 1 binding e a validação não dispara
        o erro de declarative-sem-bindings."""
        md = (
            "---\n"
            "id: urn:skill:geral:subagent:consultar-cep\n"
            "version: 0.1.0\n"
            "kind: subagent\n"
            "execution_mode: declarative\n"
            "---\n"
            "\n"
            "# Consultar CEP\n"
            "\n"
            "## Purpose\n"
            "Consulta CEP.\n"
            "\n"
            "## Tool Bindings\n"
            "_Não usa MCP._\n"
            "\n"
            "---\n"
            "\n"
            "## API Bindings\n"
            "\n"
            "```yaml\n"
            "  - id: 1ad339ec-ae07-41c0-bd6b-9874bd7de010\n"
            "    connector: bb48804e-0e73-4de7-9af0-1244b47bc9f6\n"
            "    connector_id: bb48804e-0e73-4de7-9af0-1244b47bc9f6\n"
            "    name: Consultar CEP\n"
            "    method: GET\n"
            "    path: /api/cep/v1/{cep}\n"
            "```\n"
            "\n---\n"
            "\n"
            "## Execution Profile\n"
            "\n"
            "```yaml\n"
            "mode: standard\n"
            "```\n"
        )
        result = parse_skill_md(md)
        assert result.api_bindings_parsed, (
            "binding sumiu — fix do fence_close não pegou. "
            f"errors={result.validation_errors}"
        )
        assert result.api_bindings_parsed[0]["id"] == "1ad339ec-ae07-41c0-bd6b-9874bd7de010"
        # E o erro específico do bug não aparece mais
        for err in result.validation_errors:
            assert "exige ## API Bindings OU ## Data Tables" not in err, (
                f"erro de declarative-sem-bindings ainda aparece: {err}"
            )

    def test_wizard_skill_evidence_policy_sources_survive(self):
        """SKILL.md como o wizard "IA, me ajude" gera para um Especialista com
        Fontes RAG (achado do teste E2E do Cenário A, 2026-07-08): `## Evidence
        Policy` com fence YAML + HR `---` antes de `## Execution Profile`. Antes
        do fix, `evidence_policy_parsed.sources` vinha vazio → o especialista
        pulava o RAG em pipeline. Agora deve trazer o KB declarado."""
        md = (
            "---\n"
            "id: urn:skill:atendimento:subagent:acolhimento-sentimento-negativo\n"
            "version: 0.1.0\n"
            "kind: subagent\n"
            "---\n\n"
            "# Especialista de Acolhimento\n\n"
            "## Purpose\nAcolhe clientes com sentimento negativo.\n\n"
            "## Evidence Policy\n"
            "```yaml\n"
            "sources:\n"
            "  - ee1b4458-5237-433c-b099-8ba3dc3737bd   # Acolhimento (internal)\n"
            "```\n\n"
            "---\n\n"
            "## Execution Profile\n"
            "```yaml\n"
            "mode: standard\n"
            "```\n"
        )
        result = parse_skill_md(md)
        assert result.evidence_policy_parsed.get("sources") == [
            "ee1b4458-5237-433c-b099-8ba3dc3737bd"
        ], (
            "evidence_policy.sources sumiu no SKILL.md do wizard — o RAG do "
            f"especialista NÃO dispararia em pipeline. parsed={result.evidence_policy_parsed!r}"
        )
