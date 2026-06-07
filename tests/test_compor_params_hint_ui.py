"""Slice UI (2026-06-07): o Composer exibe, sob cada regra, os parâmetros que o
destino declara (## Inputs) — "🔧 X requer: cep". Dados vêm de um endpoint leve
(/wizard/destination-inputs) que reusa _collect_destination_inputs.

Frontend Alpine não roda no pytest → smoke de fonte (padrão de TestUiAndBackendWiring).
"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


class TestDestinationInputsEndpoint:
    def test_endpoint_and_model_exist(self):
        src = (_ROOT / "app" / "routes" / "wizard.py").read_text(encoding="utf-8")
        assert '@router.post("/destination-inputs")' in src
        assert "class WizardDestinationInputsRequest" in src
        assert "_collect_destination_inputs(data.agents)" in src


class TestComposerParamHintMarkup:
    def _agent_form(self) -> str:
        return (_ROOT / "app" / "templates" / "pages" / "agent_form.html").read_text(encoding="utf-8")

    def test_data_and_methods_present(self):
        src = self._agent_form()
        assert "destParams: {}" in src
        assert "async loadDestParams()" in src
        assert "paramsForTarget(t)" in src

    def test_hint_rendered_under_rule(self):
        src = self._agent_form()
        # hint só aparece quando o destino tem params
        assert 'x-show="paramsForTarget(rule.target).length"' in src
        assert "requer:" in src

    def test_loaddestparams_called_after_agents(self):
        src = self._agent_form()
        assert "this.loadDestParams();" in src
        assert "/api/v1/wizard/destination-inputs" in src
