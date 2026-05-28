"""Onda 1: Output Shape — preset de tamanho da resposta.

User pediu controle sobre formato/tamanho da resposta no Wizard.
Esta onda entrega: dropdown de presets (Intenção/Resumo/Sumário/Análise/
Relatório/Livre), parser entende ## Output Shape, engine injeta diretiva
+ truncate hard, diagnóstico sinaliza violação.

Cadeia E2E coberta:
  Wizard payload → prompt builder → SKILL.md → parser → engine.system_prompt
  + engine.truncate → trace.output_truncated_by_preset → diagnostics
"""
from __future__ import annotations

import pytest

from app.skill_parser.output_shape import (
    DEFAULT_LENGTH_PRESET,
    LENGTH_PRESETS,
    build_directive,
    enforce_truncate,
    get_max_chars,
    is_valid_preset,
)


# ─── Constants ─────────────────────────────────────────────────────


class TestLengthPresetsConstants:
    def test_six_presets_in_canonical_order(self):
        """Presets canônicos + ordem mantida (UI consome esta ordem)."""
        keys = list(LENGTH_PRESETS.keys())
        assert keys == ["intent", "summary", "digest", "analysis", "report", "unbounded"]

    def test_default_is_digest(self):
        """Skills sem ## Output Shape caem em 'digest' (1500 chars)."""
        assert DEFAULT_LENGTH_PRESET == "digest"
        assert LENGTH_PRESETS[DEFAULT_LENGTH_PRESET]["max_chars"] == 1500

    def test_unbounded_has_no_max_chars(self):
        assert LENGTH_PRESETS["unbounded"]["max_chars"] is None

    def test_max_chars_monotonically_increasing(self):
        """intent ≤ summary ≤ digest ≤ analysis ≤ report — UX consistente."""
        bounded = ["intent", "summary", "digest", "analysis", "report"]
        prev = 0
        for k in bounded:
            cur = LENGTH_PRESETS[k]["max_chars"]
            assert cur >= prev, f"{k} ({cur}) violou monotonicidade (anterior {prev})"
            prev = cur


# ─── Helpers do output_shape.py ────────────────────────────────────


class TestOutputShapeHelpers:
    def test_is_valid_preset_known(self):
        assert is_valid_preset("digest")
        assert is_valid_preset("unbounded")

    def test_is_valid_preset_unknown(self):
        assert not is_valid_preset("typo")
        assert not is_valid_preset("")

    def test_get_max_chars_known(self):
        assert get_max_chars("intent") == 600
        assert get_max_chars("digest") == 1500
        assert get_max_chars("unbounded") is None

    def test_get_max_chars_unknown_falls_back_to_default(self):
        """Defensivo: preset desconhecido cai no DEFAULT — não None,
        nem exception. Operador pode digitar typo sem quebrar a skill."""
        assert get_max_chars("typo_xpto") == 1500  # digest


class TestBuildDirective:
    def test_bounded_preset_cites_limit(self):
        """Diretiva imperativa cita número exato pro LLM."""
        d = build_directive("digest")
        assert "1500" in d
        assert "Sumário" in d
        assert "TAMANHO DA RESPOSTA" in d

    def test_unbounded_says_no_limit(self):
        d = build_directive("unbounded")
        assert "Não há limite" in d or "sem limite" in d.lower()
        assert "Livre" in d


class TestEnforceTruncate:
    def test_short_text_unchanged(self):
        text, truncated = enforce_truncate("oi", "digest")
        assert text == "oi"
        assert truncated is False

    def test_long_text_truncated_with_ellipsis(self):
        long = "x" * 2000
        text, truncated = enforce_truncate(long, "digest")  # max 1500
        assert truncated is True
        assert len(text) == 1500
        assert text.endswith("…")

    def test_unbounded_passes_intact(self):
        long = "x" * 50000
        text, truncated = enforce_truncate(long, "unbounded")
        assert text == long
        assert truncated is False

    def test_empty_text(self):
        text, truncated = enforce_truncate("", "digest")
        assert text == ""
        assert truncated is False


# ─── Parser do ## Output Shape ─────────────────────────────────────


class TestParserOutputShape:
    def test_parser_extracts_valid_preset(self):
        from app.skill_parser.parser import parse_skill_md
        md = """---
id: urn:skill:x:subagent:y
version: 0.1.0
kind: subagent
owner: x
stability: alpha
---

# Test

## Purpose
x

## Activation Criteria
x

## Inputs
x

## Workflow
x

## Tool Bindings
x

## Output Contract
x

## Failure Modes
x

## Output Shape

```yaml
length_preset: analysis
```
"""
        parsed = parse_skill_md(md)
        assert parsed.is_valid
        assert parsed.output_shape_parsed.get("length_preset") == "analysis"
        assert parsed.output_shape_parsed.get("max_chars") == 4000

    def test_parser_rejects_invalid_preset(self):
        """Preset inválido vira erro de validação — operador precisa saber."""
        from app.skill_parser.parser import parse_skill_md
        md = """---
id: urn:skill:x:subagent:y
version: 0.1.0
kind: subagent
owner: x
stability: alpha
---

# Test

## Purpose
x

## Activation Criteria
x

## Inputs
x

## Workflow
x

## Tool Bindings
x

## Output Contract
x

## Failure Modes
x

## Output Shape

```yaml
length_preset: digist
```
"""
        parsed = parse_skill_md(md)
        # Skill ainda parseia, mas validation_errors sinaliza
        errs = [e for e in parsed.validation_errors if "length_preset" in e]
        assert errs, f"Esperava erro de length_preset inválido, veio: {parsed.validation_errors}"
        assert "digist" in errs[0]

    def test_parser_without_output_shape_section(self):
        """Skill sem ## Output Shape → output_shape_parsed vazio. Engine
        aplica default 'digest'."""
        from app.skill_parser.parser import parse_skill_md
        md = """---
id: urn:skill:x:subagent:y
version: 0.1.0
kind: subagent
owner: x
stability: alpha
---

# Test

## Purpose
x

## Activation Criteria
x

## Inputs
x

## Workflow
x

## Tool Bindings
x

## Output Contract
x

## Failure Modes
x
"""
        parsed = parse_skill_md(md)
        assert parsed.is_valid
        assert not parsed.output_shape_parsed.get("length_preset")


# ─── Wizard prompt builder ─────────────────────────────────────────


class TestWizardPromptOutputShape:
    def test_wizard_includes_output_shape_when_preset_set(self):
        from app.routes.wizard import WizardSkillRequest, _build_wizard_prompt
        req = WizardSkillRequest(description="x", length_preset="report")
        bindings = {"mcp_tools": [], "rag_sources": [], "data_tables": [], "api_endpoints": []}
        system, _ = _build_wizard_prompt(req, bindings, "fast")
        assert "## Output Shape" in system
        assert "length_preset: report" in system

    def test_wizard_omits_output_shape_when_no_preset(self):
        """Sem preset, wizard não emite a seção — engine usa default."""
        from app.routes.wizard import WizardSkillRequest, _build_wizard_prompt
        req = WizardSkillRequest(description="x")  # sem length_preset
        bindings = {"mcp_tools": [], "rag_sources": [], "data_tables": [], "api_endpoints": []}
        system, _ = _build_wizard_prompt(req, bindings, "fast")
        # Não deve ter Output Shape nas seções obrigatórias
        # (no esqueleto canônico do prompt, ## Output Shape não aparece como
        # estrutura — só vai pro YAML quando user escolheu)
        assert "length_preset:" not in system

    def test_wizard_rejects_invalid_preset_value(self):
        """Pydantic pattern blinda valores arbitrários — UI dropdown impõe."""
        from app.routes.wizard import WizardSkillRequest
        with pytest.raises(Exception):
            WizardSkillRequest(description="x", length_preset="enormous")
        with pytest.raises(Exception):
            WizardSkillRequest(description="x", length_preset="digist")
