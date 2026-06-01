"""Regressão do bug do {placeholder} no path do API binding.

User reportou (2026-06-01): invocou via slash o binding "Consultar CEP" com
input `cep=13211740` (8 chars válidos). BrasilAPI respondeu HTTP 400 com
"CEP informado possui menos do que 8 caracteres" — recebeu literal `{cep}`
(5 chars) porque o engine só interpola Jinja `{{ }}` e o parser persiste
path em estilo brace-único `{name}` (RFC 6570 / OpenAPI).

Bug gêmeo já corrigido na UI "Testar" do connector (PR #236), mas o slash
invoke do workspace passou despercebido. Fix: helper
`_resolve_path_placeholders` substitui `{name}` por `scope.inputs[name]`
antes do `_render`.
"""
from __future__ import annotations

import pytest

from app.agents.declarative_engine import _resolve_path_placeholders


class TestResolvePathPlaceholders:
    def test_resolves_single_placeholder_from_inputs(self):
        """Caso central do bug: `{cep}` → valor real antes do httpx."""
        path = "/api/cep/v1/{cep}"
        scope = {"inputs": {"cep": "13211740"}}
        assert _resolve_path_placeholders(path, scope) == "/api/cep/v1/13211740"

    def test_resolves_multiple_placeholders(self):
        path = "/v1/{tenant}/users/{user_id}"
        scope = {"inputs": {"tenant": "acme", "user_id": "42"}}
        out = _resolve_path_placeholders(path, scope)
        assert out == "/v1/acme/users/42"

    def test_numeric_input_stringified(self):
        """Input numérico vira string na URL (ninguém quer `/api/v1/None` ou TypeError)."""
        path = "/v1/items/{id}"
        scope = {"inputs": {"id": 99}}
        assert _resolve_path_placeholders(path, scope) == "/v1/items/99"

    def test_unresolved_placeholder_kept_literal(self):
        """Placeholder não-existente fica literal — facilita debug em vez de
        virar string vazia silenciosa."""
        path = "/v1/{missing}"
        scope = {"inputs": {"other": "x"}}
        assert _resolve_path_placeholders(path, scope) == "/v1/{missing}"

    def test_no_placeholder_returns_path_intact(self):
        path = "/api/health"
        assert _resolve_path_placeholders(path, {"inputs": {}}) == "/api/health"

    def test_non_string_path_passthrough(self):
        """Defesa contra input inesperado — não quebra se vier None ou dict."""
        assert _resolve_path_placeholders(None, {}) is None  # type: ignore[arg-type]
        assert _resolve_path_placeholders(42, {}) == 42       # type: ignore[arg-type]

    def test_falls_back_to_scope_root_when_inputs_missing(self):
        """Compat: se o template usa `{tenant}` mas o scope só tem `tenant`
        no top-level (não dentro de `inputs`), ainda resolve."""
        scope = {"tenant": "acme"}
        assert _resolve_path_placeholders("/v1/{tenant}/x", scope) == "/v1/acme/x"

    def test_jinja_template_passes_through_untouched(self):
        """Jinja `{{ inputs.cep }}` segue funcionando — o engine o trata
        depois. Este helper só toca em `{name}` brace-único."""
        path = "/api/cep/v1/{{ inputs.cep }}"
        scope = {"inputs": {"cep": "13211740"}}
        # Helper não substitui `{{ }}` (só `{name}` puro)
        out = _resolve_path_placeholders(path, scope)
        assert "{{ inputs.cep }}" in out

    def test_empty_braces_not_touched(self):
        """`{}` literal (objeto JSON vazio) não é placeholder válido."""
        path = "/api/v1/echo?body={}"
        assert _resolve_path_placeholders(path, {"inputs": {}}) == path

    def test_invalid_identifier_not_touched(self):
        """`{not-a-id}` (com hífen) não é identificador Python válido — fica."""
        path = "/v1/{has-hyphen}"
        assert _resolve_path_placeholders(path, {"inputs": {"has-hyphen": "x"}}) == path


class TestIntegrationWithPlanBinding:
    """E2E: o plan gerado por _plan_binding tem o path resolvido (sem {placeholder}).

    Cobre o caminho REAL que o slash invoke usa (_invoke_api_binding_direct
    delega para execute_declarative que chama _plan_binding), não só o
    helper isolado.
    """

    @pytest.mark.asyncio
    async def test_plan_binding_resolves_path_from_inputs(self, monkeypatch):
        """O bug reportado: binding com path=`/api/cep/v1/{cep}` + inputs.cep
        deve resultar em plan.path=`/api/cep/v1/<valor>`."""
        from app.agents import declarative_engine as eng

        # Mock do connector lookup pra não tocar em DB
        async def fake_resolve_connector(ref):
            return {
                "id": "c-brasilapi",
                "base_url": "https://brasilapi.com.br",
                "timeout_ms": 30000,
                "auth": {},
            }
        monkeypatch.setattr(eng, "_resolve_connector", fake_resolve_connector)

        binding = {
            "id": "ep-cep",
            "connector": "c-brasilapi",
            "method": "GET",
            "path": "/api/cep/v1/{cep}",
        }
        scope = {"inputs": {"cep": "13211740"}}
        plan, err = await eng._plan_binding(binding, scope, lenient=False)
        assert err is None, f"_plan_binding reportou erro: {err}"
        assert plan is not None
        assert plan["path"] == "/api/cep/v1/13211740", (
            f"path não foi resolvido. plan['path']={plan['path']!r}"
        )
