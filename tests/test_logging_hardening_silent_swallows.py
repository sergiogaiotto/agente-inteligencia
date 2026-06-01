"""User pediu reforço (2026-06-01): todo erro útil para troubleshooting tem
que ir para `errors.log` (handler já configurado em logging_setup.py).

Antes desta PR, 4 fluxos críticos engoliam exceptions silenciosamente:

1. `parser.py:_parse_api_bindings/_parse_data_tables/_parse_evidence_policy
   /_parse_output_shape` — `except yaml.YAMLError: return []` sem log.
   Diagnóstico do bug #244 (fence_close greedy) levou 1 PR só para achar.
2. `engine.py:_extract_json_schema_from_contract` — `except (json.JSON
   DecodeError, ValueError): return None` silencioso.
3. `request_context.py:_capture_body_preview` — `except: return ""` em 2
   pontos (body read + json decode).
4. `declarative_engine.py:_plan_binding` — exception de render Jinja só
   ia para o `errors` da pipeline, nunca para errors.log.

Cada teste captura logs via caplog e valida que (a) o evento esperado
aparece, (b) extras carregam contexto suficiente para debug.
"""
from __future__ import annotations

import json
import logging

import pytest


# ─── parser: YAML malformado vai para log ──────────────────────────


class TestParserYamlErrorsLogged:
    def test_api_bindings_yaml_invalid_logged(self, caplog):
        from app.skill_parser.parser import _parse_api_bindings

        bad_yaml = "```yaml\n- id: x\n  : missing key\n```"
        with caplog.at_level(logging.WARNING, logger="app.skill_parser.parser"):
            out = _parse_api_bindings(bad_yaml)

        assert out == []
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "skill_parser.yaml_invalid" in events, (
            f"warning não foi emitido. events={events}"
        )
        # Extras incluem section e error_type
        record = next(r for r in caplog.records if getattr(r, "event", None) == "skill_parser.yaml_invalid")
        assert record.section == "API Bindings"
        assert record.error_type  # nome da exception, p.ex. "ScannerError"

    def test_data_tables_yaml_invalid_logged(self, caplog):
        from app.skill_parser.parser import _parse_data_tables

        bad_yaml = "```yaml\n- id: x\n  table_ref: urn:t:y\n  : bad\n```"
        with caplog.at_level(logging.WARNING, logger="app.skill_parser.parser"):
            out = _parse_data_tables(bad_yaml)

        assert out == []
        sections = [getattr(r, "section", None) for r in caplog.records]
        assert "Data Tables" in sections

    def test_evidence_policy_yaml_invalid_logged(self, caplog):
        from app.skill_parser.parser import _parse_evidence_policy

        bad_yaml = "```yaml\nsources:\n  - x\n  : bad\n```"
        with caplog.at_level(logging.WARNING, logger="app.skill_parser.parser"):
            out = _parse_evidence_policy(bad_yaml)

        # Volta {raw: ...} (fallback), e o log saiu
        assert "raw" in out
        sections = [getattr(r, "section", None) for r in caplog.records]
        assert "Evidence Policy" in sections

    def test_output_shape_yaml_invalid_logged(self, caplog):
        from app.skill_parser.parser import _parse_output_shape

        bad_yaml = "```yaml\nlength_preset: foo\n  : bad\n```"
        with caplog.at_level(logging.WARNING, logger="app.skill_parser.parser"):
            out = _parse_output_shape(bad_yaml)

        assert out == {}
        sections = [getattr(r, "section", None) for r in caplog.records]
        assert "Output Shape" in sections

    def test_valid_yaml_does_not_log(self, caplog):
        """Happy path: YAML válido NÃO gera warning (não polui errors.log)."""
        from app.skill_parser.parser import _parse_api_bindings

        good = "```yaml\n- id: ep-1\n  connector: c-1\n  method: GET\n  path: /v1/x\n```"
        with caplog.at_level(logging.WARNING, logger="app.skill_parser.parser"):
            out = _parse_api_bindings(good)

        assert len(out) == 1
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "skill_parser.yaml_invalid" not in events


# ─── engine: Output Contract JSON inválido vai para log ───────────


class TestEngineJsonErrorsLogged:
    def test_invalid_json_in_output_contract_logged(self, caplog):
        from app.agents.engine import _extract_json_schema_from_contract

        bad_contract = "```json\n{ this is broken json }\n```"
        with caplog.at_level(logging.WARNING, logger="app.agents.engine"):
            out = _extract_json_schema_from_contract(bad_contract)

        assert out is None
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "engine.json_invalid" in events
        record = next(r for r in caplog.records if getattr(r, "event", None) == "engine.json_invalid")
        assert record.section == "Output Contract"
        # candidate_preview tem o que tentou parsear, truncado
        assert record.candidate_preview

    def test_schema_not_object_logged(self, caplog):
        """JSON válido mas não-objeto (ex.: array no topo) também loga."""
        from app.agents.engine import _extract_json_schema_from_contract

        weird = "```json\n[1, 2, 3]\n```"
        with caplog.at_level(logging.WARNING, logger="app.agents.engine"):
            out = _extract_json_schema_from_contract(weird)

        assert out is None
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "engine.schema_invalid" in events

    def test_empty_schema_logged(self, caplog):
        """Objeto sem `type`/`properties`/`$ref` — não é schema válido."""
        from app.agents.engine import _extract_json_schema_from_contract

        empty = "```json\n{\"title\": \"X\"}\n```"
        with caplog.at_level(logging.WARNING, logger="app.agents.engine"):
            out = _extract_json_schema_from_contract(empty)

        assert out is None
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "engine.schema_invalid" in events

    def test_valid_schema_does_not_log(self, caplog):
        from app.agents.engine import _extract_json_schema_from_contract

        good = '```json\n{"type": "object", "properties": {"x": {"type": "string"}}}\n```'
        with caplog.at_level(logging.WARNING, logger="app.agents.engine"):
            out = _extract_json_schema_from_contract(good)

        assert out is not None
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "engine.json_invalid" not in events
        assert "engine.schema_invalid" not in events


# ─── request_context: body indisponível e JSON malformado logados ──


class TestRequestContextBodyFailuresLogged:
    def test_body_json_decode_failure_logged(self, caplog):
        """JSON inválido em request com Content-Type application/json:
        log warning, fallback texto cru."""
        from app.core.request_context import _capture_body_preview

        class _StubRequest:
            method = "POST"
            class url:  # noqa: D106 - test stub
                path = "/api/v1/whatever"
            headers = {"content-type": "application/json"}

            async def body(self):
                return b"{not valid json"

        import asyncio
        with caplog.at_level(logging.WARNING, logger="app.api"):
            out = asyncio.run(_capture_body_preview(_StubRequest()))

        # Fallback funcionou — devolveu texto
        assert out
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "request_context.body_failed" in events


# ─── declarative_engine: erro de render template logado ──────────


class TestDeclarativeEngineTemplateFailureLogged:
    @pytest.mark.asyncio
    async def test_plan_binding_render_failure_logged(self, caplog, monkeypatch):
        from app.agents import declarative_engine as eng

        async def fake_resolve_connector(ref):
            return {"id": "c-1", "base_url": "https://x", "timeout_ms": 30000}

        monkeypatch.setattr(eng, "_resolve_connector", fake_resolve_connector)

        # Path tem Jinja inválido (sintaxe quebrada)
        binding = {
            "id": "ep-broken",
            "connector": "c-1",
            "method": "GET",
            "path": "/v1/{{ inputs.x.y.[ }}",   # Jinja malformado
        }
        with caplog.at_level(logging.WARNING, logger="app.agents.declarative_engine"):
            plan, err = await eng._plan_binding(binding, {"inputs": {}}, lenient=False)

        assert plan is None
        assert err
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "declarative.template_failed" in events
        record = next(r for r in caplog.records if getattr(r, "event", None) == "declarative.template_failed")
        assert record.binding_id == "ep-broken"
        assert record.connector == "c-1"
