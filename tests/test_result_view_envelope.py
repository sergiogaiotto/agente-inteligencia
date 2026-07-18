"""P1-B: envelope de resposta do invoke — schema_version + data (output parseado).

F8: `output` é string (prosa OU JSON serializado) → parse-duplo no cliente. Agora
`data` traz o objeto pronto quando é JSON + `output_is_json`. F10: `schema_version`
em TODAS as verbosidades. Aditivo: `output` (string) e o full verbatim preservados.
"""
from __future__ import annotations

from app.agents.result_view import (
    project_pipeline_result,
    _output_data,
    SCHEMA_VERSION,
)


class TestOutputData:
    def test_json_object_string_parsed(self):
        assert _output_data('{"a": 1}') == ({"a": 1}, True)

    def test_json_array_string_parsed(self):
        assert _output_data("[1, 2]") == ([1, 2], True)

    def test_prose_is_not_json(self):
        assert _output_data("Olá, isto é prosa livre.") == (None, False)

    def test_dict_passthrough(self):
        assert _output_data({"x": 1}) == ({"x": 1}, True)

    def test_malformed_json_treated_as_prose(self):
        assert _output_data("{not valid json") == (None, False)

    def test_empty_and_none(self):
        assert _output_data("") == (None, False)
        assert _output_data(None) == (None, False)


def _result():
    return {
        "pipeline_id": "p1",
        "interaction_id": "i1",
        "status": "completed",
        "output": '{"specification": "x"}',
        "final_state": "LogAndClose",
        "total_agents": 1,
        "completed_agents": 1,
        "duration_ms": 100,
        "pipeline_steps": [{"agent_name": "A", "status": "completed", "output": "..."}],
    }


class TestEnvelope:
    def test_minimal_has_schema_version_and_data(self):
        r = project_pipeline_result(_result(), "minimal")
        assert r["schema_version"] == SCHEMA_VERSION
        assert r["data"] == {"specification": "x"}
        assert r["output_is_json"] is True
        assert r["output"] == '{"specification": "x"}'  # string preservada
        assert "pipeline_steps" not in r

    def test_summary_has_schema_version_and_data(self):
        r = project_pipeline_result(_result(), "summary")
        assert r["schema_version"] == SCHEMA_VERSION
        assert r["data"] == {"specification": "x"}
        assert r["output_is_json"] is True
        assert "steps" in r

    def test_full_verbatim_plus_additive_fields(self):
        r = project_pipeline_result(_result(), "full")
        assert r["schema_version"] == SCHEMA_VERSION
        assert r["data"] == {"specification": "x"}
        assert r["output_is_json"] is True
        assert "pipeline_steps" in r  # full mantém verbatim (retrocompat)

    def test_summary_step_carrega_status_message_e_duration(self):
        # a UI (Playground) mostra 💬 mensagem de status + tempo por step; ambos
        # precisam sobreviver à projeção summary.
        res = _result()
        res["pipeline_steps"] = [{
            "agent_name": "Triagem", "status": "completed",
            "status_message": "Olha eu aki", "duration_ms": 42, "output": "...",
        }]
        step = project_pipeline_result(res, "summary")["steps"][0]
        assert step["status_message"] == "Olha eu aki"
        assert step["duration_ms"] == 42

    def test_summary_step_duration_none_quando_ausente(self):
        # step sem duração (ex.: pulado) → None, e a UI esconde o tempo.
        step = project_pipeline_result(_result(), "summary")["steps"][0]
        assert step["duration_ms"] is None
        assert step["status_message"] == ""

    def test_prose_output_gives_null_data(self):
        res = _result()
        res["output"] = "resposta em prosa, não JSON"
        r = project_pipeline_result(res, "summary")
        assert r["data"] is None
        assert r["output_is_json"] is False
