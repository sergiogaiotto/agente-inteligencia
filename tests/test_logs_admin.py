"""Testes dos endpoints REST de manutenção de logs.

Cobre:
- /stats: estrutura do response, disk usage, files inventory
- /tail: limite, path safety, arquivo inexistente
- /clear: trunca, role=root obrigatório, path safety
- /rotate: força rollover, role=root
- /archives DELETE: apaga arquivos antigos, preserva atuais
"""
from __future__ import annotations


import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.auth import require_user
from app.routes.logs_admin import router as logs_admin_router


@pytest.fixture
def isolated_logs(monkeypatch, tmp_path):
    """Cria pasta de logs isolada em tmp + arquivos simulados."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setenv("LOG_DIR", str(log_dir))
    # Cria os 5 arquivos canônicos com algum conteúdo
    contents = {
        "app.log": '{"ts":"2026-05-25T10:00:00Z","level":"INFO","msg":"hi"}\n' * 5,
        "tabular.log": '{"event":"x","level":"INFO"}\n' * 10,
        "api.log": '{"event":"http.response","status_code":200}\n' * 20,
        "audit.log": '{"event":"audit","actor":"u-x"}\n' * 3,
        "errors.log": '',  # vazio
    }
    for name, c in contents.items():
        (log_dir / name).write_text(c, encoding="utf-8")
    # Cria archives (backups rotacionados)
    (log_dir / "app.log.2026-05-23").write_text("old1\n" * 100, encoding="utf-8")
    (log_dir / "tabular.log.2026-05-22").write_text("old2\n" * 200, encoding="utf-8")
    return log_dir


def make_app(user):
    app = FastAPI()
    app.include_router(logs_admin_router)
    app.dependency_overrides[require_user] = lambda: user
    return app


@pytest.fixture
def root_user():
    return {"id": "u-root", "role": "root", "domains": "[]"}


@pytest.fixture
def common_user():
    return {"id": "u-comum", "role": "comum", "domains": "[]"}


# ─── /stats ──────────────────────────────────────────────────────


class TestStats:
    def test_stats_returns_5_canonical_files(self, isolated_logs, root_user):
        client = TestClient(make_app(root_user))
        r = client.get("/api/v1/observability/logs/stats")
        assert r.status_code == 200
        body = r.json()
        names = [f["name"] for f in body["files"]]
        assert set(names) == {"app", "tabular", "api", "audit", "errors"}

    def test_stats_has_disk_info(self, isolated_logs, root_user):
        client = TestClient(make_app(root_user))
        body = client.get("/api/v1/observability/logs/stats").json()
        assert "disk" in body and "free" in body["disk"]
        assert "used_pct" in body["disk"]
        assert "total_human" in body["disk"]

    def test_stats_lists_archives(self, isolated_logs, root_user):
        client = TestClient(make_app(root_user))
        body = client.get("/api/v1/observability/logs/stats").json()
        archive_names = [a["name"] for a in body["archives"]]
        assert "app.log.2026-05-23" in archive_names
        assert "tabular.log.2026-05-22" in archive_names
        assert body["totals"]["archives_count"] == 2

    def test_stats_calculates_totals(self, isolated_logs, root_user):
        client = TestClient(make_app(root_user))
        body = client.get("/api/v1/observability/logs/stats").json()
        assert body["totals"]["files_size"] > 0
        assert body["totals"]["archives_size"] > 0
        assert body["totals"]["all_size"] == body["totals"]["files_size"] + body["totals"]["archives_size"]


# ─── /tail ───────────────────────────────────────────────────────


class TestTail:
    def test_tail_returns_lines(self, isolated_logs, root_user):
        client = TestClient(make_app(root_user))
        r = client.get("/api/v1/observability/logs/tail/api?lines=5")
        assert r.status_code == 200
        body = r.json()
        assert body["file"] == "api.log"
        assert body["returned_lines"] == 5
        assert len(body["lines"]) == 5

    def test_tail_path_traversal_blocked(self, isolated_logs, root_user):
        """Path traversal pode ser bloqueado pelo router (404) OU pelo validator
        (400) — ambos são bloqueio efetivo. Defesa em profundidade está intacta:
        mesmo se o router permitisse, o _validate_log_name rejeitaria."""
        client = TestClient(make_app(root_user))
        r = client.get("/api/v1/observability/logs/tail/..%2F..%2Fetc%2Fpasswd")
        assert r.status_code in (400, 404), f"esperava 400 ou 404, got {r.status_code}"

    def test_tail_dotdot_in_name_blocked_by_validator(self, isolated_logs, root_user):
        """Se o nome 'puro' chegar ao validator, ele rejeita 400."""
        client = TestClient(make_app(root_user))
        # Path-param "passwd" (não no whitelist) → 400
        r = client.get("/api/v1/observability/logs/tail/passwd")
        assert r.status_code == 400

    def test_tail_invalid_name_rejected(self, isolated_logs, root_user):
        client = TestClient(make_app(root_user))
        r = client.get("/api/v1/observability/logs/tail/hacker")
        assert r.status_code == 400

    def test_tail_max_lines_capped(self, isolated_logs, root_user):
        client = TestClient(make_app(root_user))
        r = client.get("/api/v1/observability/logs/tail/api?lines=99999")
        # 99999 > _TAIL_MAX_LINES (1000) → FastAPI Query validation rejeita
        assert r.status_code == 422

    def test_tail_nonexistent_file_404(self, isolated_logs, root_user, tmp_path, monkeypatch):
        # Aponta LOG_DIR para outro tmp sem o arquivo
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        monkeypatch.setenv("LOG_DIR", str(empty_dir))
        client = TestClient(make_app(root_user))
        r = client.get("/api/v1/observability/logs/tail/api")
        assert r.status_code == 404


# ─── /clear ──────────────────────────────────────────────────────


class TestClear:
    def test_clear_truncates_file(self, isolated_logs, root_user):
        client = TestClient(make_app(root_user))
        size_before = (isolated_logs / "api.log").stat().st_size
        assert size_before > 0
        r = client.post("/api/v1/observability/logs/clear/api")
        assert r.status_code == 200
        assert r.json()["size_after"] == 0
        assert (isolated_logs / "api.log").stat().st_size == 0
        # Arquivo continua existindo
        assert (isolated_logs / "api.log").exists()

    def test_clear_requires_root(self, isolated_logs, common_user):
        client = TestClient(make_app(common_user))
        r = client.post("/api/v1/observability/logs/clear/api")
        assert r.status_code == 403

    def test_clear_invalid_name_rejected(self, isolated_logs, root_user):
        client = TestClient(make_app(root_user))
        r = client.post("/api/v1/observability/logs/clear/hacker")
        assert r.status_code == 400


# ─── /archives DELETE ────────────────────────────────────────────


class TestDeleteArchives:
    def test_delete_all_archives(self, isolated_logs, root_user):
        client = TestClient(make_app(root_user))
        r = client.delete("/api/v1/observability/logs/archives")
        assert r.status_code == 200
        body = r.json()
        assert body["deleted_count"] == 2
        assert body["freed_bytes"] > 0
        # Archives sumiram
        assert not (isolated_logs / "app.log.2026-05-23").exists()
        assert not (isolated_logs / "tabular.log.2026-05-22").exists()
        # Arquivos atuais PRESERVADOS
        assert (isolated_logs / "app.log").exists()
        assert (isolated_logs / "tabular.log").exists()

    def test_delete_requires_root(self, isolated_logs, common_user):
        client = TestClient(make_app(common_user))
        r = client.delete("/api/v1/observability/logs/archives")
        assert r.status_code == 403

    def test_delete_with_older_than_days_filter(self, isolated_logs, root_user):
        client = TestClient(make_app(root_user))
        # older_than_days=365 → arquivos com mtime > 1 ano. Nossos archives são
        # de ontem/anteontem (criados agora no test), então NENHUM bate.
        r = client.delete("/api/v1/observability/logs/archives?older_than_days=365")
        assert r.status_code == 200
        assert r.json()["deleted_count"] == 0
        # Archives ainda existem
        assert (isolated_logs / "app.log.2026-05-23").exists()


# ─── /rotate ─────────────────────────────────────────────────────


class TestRotate:
    def test_rotate_requires_root(self, isolated_logs, common_user):
        client = TestClient(make_app(common_user))
        r = client.post("/api/v1/observability/logs/rotate")
        assert r.status_code == 403

    def test_rotate_no_handlers_returns_empty(self, isolated_logs, root_user):
        # Em ambiente de teste sem TimedRotatingFileHandler attachado,
        # rotate retorna lista vazia (não é erro).
        client = TestClient(make_app(root_user))
        r = client.post("/api/v1/observability/logs/rotate")
        assert r.status_code == 200
        body = r.json()
        # Pode ter handlers de outros loggers; só validamos schema
        assert "rotated" in body
        assert "rotated_count" in body
        assert isinstance(body["rotated"], list)


# ─── /explain (Log Viewer 2.0 — IA, me ajuda) ────────────────────


class _FakeProvider:
    """Captura messages enviadas e devolve resposta determinística."""
    last_messages = None
    last_kwargs = None

    async def generate(self, messages, **kwargs):
        _FakeProvider.last_messages = messages
        _FakeProvider.last_kwargs = kwargs
        return {
            "content": "## Resumo\n- 1 erro detectado\n- nenhuma anomalia",
            "model": "fake-primary-model",
            "usage": {"total_tokens": 42},
        }


def _fake_get_provider(name, **kwargs):
    """Override de get_provider que captura o nome solicitado."""
    _fake_get_provider.last_name = name
    _fake_get_provider.last_kwargs = kwargs
    return _FakeProvider()


class TestExplain:
    """Endpoint POST /explain — análise IA das linhas filtradas."""

    def test_explain_calls_llm_with_lines(self, isolated_logs, root_user, monkeypatch):
        monkeypatch.setattr("app.core.llm_providers.get_provider", _fake_get_provider)
        _FakeProvider.last_messages = None
        client = TestClient(make_app(root_user))

        payload = {
            "lines": ['{"ts":"2026-05-30T20:00:00Z","level":"ERROR","msg":"boom"}'],
            "preset": "errors",
            "file_name": "app.log",
        }
        r = client.post("/api/v1/observability/logs/explain", json=payload)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "Resumo" in body["answer"]
        assert body["lines_analyzed"] == 1
        assert body["model"] == "fake-primary-model"
        # Provider recebeu system + user com a linha embutida
        msgs = _FakeProvider.last_messages
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        assert "boom" in msgs[1]["content"]

    def test_explain_preset_injects_canned_prompt(self, isolated_logs, root_user, monkeypatch):
        """Cada preset deve trazer instrução característica no prompt."""
        monkeypatch.setattr("app.core.llm_providers.get_provider", _fake_get_provider)
        client = TestClient(make_app(root_user))

        for preset, marker in [
            ("summary", "Resuma"),
            ("errors", "Identifique"),
            ("anomalies", "padrões anormais"),
            ("hypothesis", "hipóteses"),
        ]:
            _FakeProvider.last_messages = None
            r = client.post("/api/v1/observability/logs/explain", json={
                "lines": ['{"level":"INFO"}'], "preset": preset, "file_name": "app.log",
            })
            assert r.status_code == 200
            user_content = _FakeProvider.last_messages[1]["content"]
            assert marker in user_content, f"preset={preset} sem marker '{marker}'"

    def test_explain_falls_back_to_summary_when_no_preset_no_question(
        self, isolated_logs, root_user, monkeypatch,
    ):
        monkeypatch.setattr("app.core.llm_providers.get_provider", _fake_get_provider)
        _FakeProvider.last_messages = None
        client = TestClient(make_app(root_user))

        r = client.post("/api/v1/observability/logs/explain", json={
            "lines": ['{"level":"INFO"}'],
        })
        assert r.status_code == 200
        assert "Resuma" in _FakeProvider.last_messages[1]["content"]

    def test_explain_free_question_passes_through(self, isolated_logs, root_user, monkeypatch):
        monkeypatch.setattr("app.core.llm_providers.get_provider", _fake_get_provider)
        _FakeProvider.last_messages = None
        client = TestClient(make_app(root_user))

        r = client.post("/api/v1/observability/logs/explain", json={
            "lines": ['{"level":"INFO"}'],
            "question": "Por que duration_ms aumentou às 14h?",
        })
        assert r.status_code == 200
        assert "duration_ms aumentou" in _FakeProvider.last_messages[1]["content"]

    def test_explain_rejects_empty_lines(self, isolated_logs, root_user):
        client = TestClient(make_app(root_user))
        r = client.post("/api/v1/observability/logs/explain", json={"lines": []})
        assert r.status_code == 422

    def test_explain_rejects_too_many_lines(self, isolated_logs, root_user):
        client = TestClient(make_app(root_user))
        r = client.post("/api/v1/observability/logs/explain", json={
            "lines": ['{"x":1}'] * 501,
        })
        assert r.status_code == 422

    def test_explain_rejects_invalid_preset(self, isolated_logs, root_user):
        client = TestClient(make_app(root_user))
        r = client.post("/api/v1/observability/logs/explain", json={
            "lines": ['{"level":"INFO"}'],
            "preset": "invalido",
        })
        assert r.status_code == 422

    def test_explain_502_when_llm_fails(self, isolated_logs, root_user, monkeypatch):
        """Erro do provider vira 502 para o cliente — não 500 — porque é
        falha de upstream LLM, não bug da aplicação."""
        class _BrokenProvider:
            async def generate(self, messages, **kwargs):
                raise RuntimeError("llm exploded")

        monkeypatch.setattr(
            "app.core.llm_providers.get_provider",
            lambda name, **kw: _BrokenProvider(),
        )
        client = TestClient(make_app(root_user))
        r = client.post("/api/v1/observability/logs/explain", json={
            "lines": ['{"level":"INFO"}'],
        })
        assert r.status_code == 502
        assert "llm exploded" in r.text

    def test_explain_uses_primary_provider_from_settings(
        self, isolated_logs, root_user, monkeypatch,
    ):
        """Provider solicitado segue settings.primary_provider quando definido."""
        from app.core.config import get_settings
        s = get_settings()
        monkeypatch.setattr(s, "primary_provider", "azure", raising=False)
        monkeypatch.setattr("app.core.llm_providers.get_provider", _fake_get_provider)

        client = TestClient(make_app(root_user))
        r = client.post("/api/v1/observability/logs/explain", json={
            "lines": ['{"x":1}'],
        })
        assert r.status_code == 200
        assert _fake_get_provider.last_name == "azure"
