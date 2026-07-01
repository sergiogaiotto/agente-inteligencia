"""Testes do logging estruturado da Onda Observabilidade.

Cobre:
- JsonFormatter: timestamp ISO 8601 UTC, level, logger, msg, extras
- Context vars (request_id, trace_id, user_id) injetados quando setados
- PII redaction em dicts aninhados
- Exception serializada com type/message/traceback
- TextFormatter (legível pra dev)
- setup_logging idempotente
- Handlers de arquivo escrevem em logs/*.log com filtro por logger
- errors.log filtra só ERROR+
- RequestContextMiddleware injeta request_id, propaga trace, loga req/resp
"""
from __future__ import annotations

import io
import json
import logging
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.logging_setup import (
    JsonFormatter,
    TextFormatter,
    request_id_var,
    setup_logging,
    trace_id_var,
    user_id_var,
)
from app.core.request_context import (
    RequestContextMiddleware,
    _validate_or_generate,
    install_request_context_middleware,
)


# ─── 1. JsonFormatter ─────────────────────────────────────────────


class TestJsonFormatter:
    @pytest.fixture(autouse=True)
    def _reset_ctx(self):
        # Garantir contexto limpo entre tests
        request_id_var.set("")
        trace_id_var.set("")
        user_id_var.set("")
        yield

    def _format(self, fmt, level=logging.INFO, msg="x", extra=None, exc_info=None):
        rec = logging.LogRecord(
            name="test", level=level, pathname="x.py", lineno=1,
            msg=msg, args=None, exc_info=exc_info,
        )
        if extra:
            for k, v in extra.items():
                setattr(rec, k, v)
        return json.loads(fmt.format(rec))

    def test_basic_fields(self):
        out = self._format(JsonFormatter(), msg="hello")
        assert out["msg"] == "hello"
        assert out["level"] == "INFO"
        assert out["logger"] == "test"
        assert "T" in out["ts"] and out["ts"].endswith("Z")

    def test_extras_become_top_level(self):
        out = self._format(JsonFormatter(),
                           extra={"event": "tabular.x", "rows": 42, "table_id": "t-1"})
        assert out["event"] == "tabular.x"
        assert out["rows"] == 42
        assert out["table_id"] == "t-1"

    def test_request_id_injected_when_set(self):
        request_id_var.set("req_abc")
        out = self._format(JsonFormatter())
        assert out["request_id"] == "req_abc"

    def test_trace_id_and_user_id_injected(self):
        trace_id_var.set("cli_xyz")
        user_id_var.set("u-root")
        out = self._format(JsonFormatter())
        assert out["trace_id"] == "cli_xyz"
        assert out["user_id"] == "u-root"

    def test_context_vars_absent_when_unset(self):
        out = self._format(JsonFormatter())
        assert "request_id" not in out
        assert "trace_id" not in out
        assert "user_id" not in out

    def test_pii_redaction_in_nested_dict(self):
        out = self._format(
            JsonFormatter(),
            extra={"auth": {"password": "hunter2", "user": "alice", "token": "secret123"}},
        )
        assert out["auth"]["password"] == "***REDACTED***"
        assert out["auth"]["token"] == "***REDACTED***"
        assert out["auth"]["user"] == "alice"

    def test_pii_redaction_recursive(self):
        out = self._format(
            JsonFormatter(),
            extra={"req": {"headers": {"Authorization": "Bearer xxx", "Content-Type": "json"}}},
        )
        assert out["req"]["headers"]["Authorization"] == "***REDACTED***"
        assert out["req"]["headers"]["Content-Type"] == "json"

    def test_exception_serialized(self):
        try:
            raise ValueError("boom")
        except ValueError:
            import sys
            out = self._format(JsonFormatter(), level=logging.ERROR,
                               msg="caught", exc_info=sys.exc_info())
        assert out["exception"]["type"] == "ValueError"
        assert out["exception"]["message"] == "boom"
        assert "Traceback" in out["exception"]["traceback"]


# ─── 2. TextFormatter ────────────────────────────────────────────


class TestTextFormatter:
    def test_includes_request_id_prefix(self):
        request_id_var.set("req_abcdef12")
        rec = logging.LogRecord(
            name="x", level=logging.INFO, pathname="x.py", lineno=1,
            msg="hi", args=None, exc_info=None,
        )
        out = TextFormatter().format(rec)
        assert "req_abcd" in out  # primeiros 8 chars

    def test_includes_extras_as_suffix(self):
        request_id_var.set("")
        rec = logging.LogRecord(
            name="x", level=logging.INFO, pathname="x.py", lineno=1,
            msg="hi", args=None, exc_info=None,
        )
        setattr(rec, "event", "test.evt")
        setattr(rec, "rows", 42)
        out = TextFormatter().format(rec)
        assert "event=test.evt" in out
        assert "rows=42" in out


# ─── 3. setup_logging ────────────────────────────────────────────


class TestSetupLogging:
    def test_idempotent(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
        monkeypatch.setenv("LOG_FILE_ENABLED", "1")
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        info1 = setup_logging(force=True)
        info2 = setup_logging(force=False)
        assert info2.get("already_setup") is True

    def test_creates_log_files(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
        monkeypatch.setenv("LOG_FILE_ENABLED", "1")
        setup_logging(force=True)
        logger = logging.getLogger("tabular.test_create")
        logger.info("smoke", extra={"event": "test.smoke", "v": 1})
        # Flush
        for h in logging.getLogger().handlers:
            h.flush()
        tabular_log = tmp_path / "logs" / "tabular.log"
        assert tabular_log.exists()
        content = tabular_log.read_text(encoding="utf-8")
        assert "test_create" in content
        assert '"v": 1' in content

    def test_errors_log_filters_only_error_plus(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
        monkeypatch.setenv("LOG_FILE_ENABLED", "1")
        setup_logging(force=True)
        logger = logging.getLogger("anything")
        logger.info("info_msg")
        logger.error("error_msg", extra={"event": "test.err"})
        for h in logging.getLogger().handlers:
            h.flush()
        errors_log = (tmp_path / "logs" / "errors.log").read_text(encoding="utf-8")
        assert "error_msg" in errors_log
        assert "info_msg" not in errors_log


# ─── 4. Request middleware ───────────────────────────────────────


class TestRequestIdValidation:
    def test_accepts_valid(self):
        assert _validate_or_generate("req_abc123") == "req_abc123"
        assert _validate_or_generate("cli_xyz_456") == "cli_xyz_456"

    def test_generates_when_missing(self):
        rid = _validate_or_generate(None)
        assert rid.startswith("req_")
        assert len(rid) >= 4

    def test_generates_when_invalid(self):
        # Caracteres não permitidos → gera novo
        rid = _validate_or_generate("'; DROP TABLE--")
        assert rid.startswith("req_")

    def test_generates_when_too_short(self):
        rid = _validate_or_generate("ab")  # < 4 chars
        assert rid.startswith("req_")


class TestRequestContextMiddleware:
    def _make_app(self):
        app = FastAPI()
        install_request_context_middleware(app)

        @app.get("/echo")
        def echo():
            return {"request_id": request_id_var.get(), "trace_id": trace_id_var.get()}

        @app.get("/boom")
        def boom():
            raise ValueError("test")

        return app

    def test_generates_request_id_when_missing(self):
        client = TestClient(self._make_app(), raise_server_exceptions=False)
        r = client.get("/echo")
        assert r.status_code == 200
        body = r.json()
        assert body["request_id"].startswith("req_")

    def test_echoes_request_id_header_back(self):
        client = TestClient(self._make_app(), raise_server_exceptions=False)
        r = client.get("/echo", headers={"X-Request-Id": "req_provided123"})
        assert r.headers["X-Request-Id"] == "req_provided123"
        assert r.json()["request_id"] == "req_provided123"

    def test_picks_up_client_trace_id(self):
        client = TestClient(self._make_app(), raise_server_exceptions=False)
        r = client.get("/echo", headers={"X-Client-Trace-Id": "cli_abc12345"})
        assert r.json()["trace_id"] == "cli_abc12345"

    def test_invalid_trace_id_rejected(self):
        client = TestClient(self._make_app(), raise_server_exceptions=False)
        r = client.get("/echo", headers={"X-Client-Trace-Id": "'; DROP--"})
        assert r.json()["trace_id"] == ""  # rejeitado, fica vazio

    def test_5xx_exception_logged(self, caplog):
        client = TestClient(self._make_app(), raise_server_exceptions=False)
        with caplog.at_level(logging.ERROR, logger="app.api"):
            r = client.get("/boom")
        assert r.status_code == 500
        # Pelo menos 1 log com event=http.exception
        events = [getattr(rec, "event", None) for rec in caplog.records]
        assert "http.exception" in events


# ─── request_received: visibilidade default + body/query logging ───
# Verifica que GET/POST/PUT/PATCH chegando geram log INFO com payload
# redactado. Antes era DEBUG (invisível em LOG_LEVEL=INFO default).


class TestRequestReceivedLogging:
    def _make_app(self):
        app = FastAPI()
        install_request_context_middleware(app)

        @app.get("/items")
        def list_items(q: str = "", token: str = ""):
            return {"q": q, "token_seen": bool(token)}

        @app.post("/items")
        def create_item(payload: dict):
            return {"created": True}

        @app.post("/upload")
        async def upload(): return {"ok": True}

        return app

    def test_get_logs_query_params_with_pii_redacted(self, caplog):
        client = TestClient(self._make_app())
        with caplog.at_level(logging.INFO, logger="app.api"):
            r = client.get("/items?q=foo&token=supersecret123")
        assert r.status_code == 200
        # Acha o request_received
        received = [r for r in caplog.records if getattr(r, "event", None) == "http.request"]
        assert received, "request_received não foi logado em INFO"
        qp = received[0].query_params
        assert qp["q"] == "foo"
        assert qp["token"] == "***REDACTED***"

    def test_post_logs_body_preview_with_pii_redacted(self, caplog):
        client = TestClient(self._make_app())
        with caplog.at_level(logging.INFO, logger="app.api"):
            r = client.post("/items", json={"name": "Foo", "password": "hunter2"})
        assert r.status_code == 200
        received = [r for r in caplog.records if getattr(r, "event", None) == "http.request"]
        assert received
        body_preview = getattr(received[0], "body_preview", "")
        assert "Foo" in body_preview
        assert "hunter2" not in body_preview, "Senha vazou no log!"
        assert "REDACTED" in body_preview

    def test_post_body_still_readable_by_handler(self):
        """Smoke crítico: ler body no middleware NÃO pode quebrar o handler
        downstream — o body precisa estar cacheado pro endpoint consumir."""
        client = TestClient(self._make_app())
        r = client.post("/items", json={"name": "Bar"})
        assert r.status_code == 200
        assert r.json() == {"created": True}

    def test_get_without_query_omits_field(self, caplog):
        client = TestClient(self._make_app())
        with caplog.at_level(logging.INFO, logger="app.api"):
            client.get("/items")
        received = [r for r in caplog.records if getattr(r, "event", None) == "http.request"]
        assert received
        # GET sem query → não tem query_params no extra (evita ruído)
        assert not hasattr(received[0], "query_params") or not received[0].query_params

    def test_health_path_still_silent(self, caplog):
        """is_noisy preservado — /api/health não polui mesmo com INFO."""
        app = FastAPI()
        install_request_context_middleware(app)
        @app.get("/api/health")
        def h(): return {"status": "ok"}
        client = TestClient(app)
        with caplog.at_level(logging.INFO, logger="app.api"):
            client.get("/api/health")
        received = [r for r in caplog.records if getattr(r, "event", None) == "http.request"]
        assert not received, "/api/health deveria continuar silencioso"


# ─── _resolve_user_id — cookie 'user_id' (não 'session') ──────────
# Histórico (2026-05-31): o middleware lia `cookies.get("session")`, mas o
# projeto nunca emitiu um cookie com esse nome — o cookie real sempre foi
# `user_id` (UUID em texto puro, set em /api/v1/users/login). Logs de
# requests autenticados ficavam SEM user_id silenciosamente.


class TestResolveUserIdCookie:
    def _make_app(self):
        app = FastAPI()
        install_request_context_middleware(app)

        @app.get("/whoami")
        def whoami():
            return {"user_id": user_id_var.get()}

        return app

    def test_user_id_cookie_populates_context_var(self):
        """Cookie de sessão ASSINADO é verificado e o UUID propaga para os logs."""
        from app.core.auth import sign_session
        client = TestClient(self._make_app())
        client.cookies.set("user_id", sign_session("41e3a8b8-0000-1111-2222-aaaabbbbcccc"))
        r = client.get("/whoami")
        assert r.status_code == 200
        assert r.json()["user_id"] == "41e3a8b8-0000-1111-2222-aaaabbbbcccc"

    def test_forged_raw_cookie_does_not_populate_context_var(self):
        """Cookie forjado (UUID cru, sem assinatura) NÃO é aceito — log fica anônimo."""
        client = TestClient(self._make_app())
        client.cookies.set("user_id", "41e3a8b8-0000-1111-2222-aaaabbbbcccc")
        r = client.get("/whoami")
        assert r.status_code == 200
        assert r.json()["user_id"] == ""

    def test_x_user_id_header_takes_precedence(self):
        """Header X-User-Id (server-to-server) vence o cookie."""
        client = TestClient(self._make_app())
        client.cookies.set("user_id", "from-cookie-uuid")
        r = client.get("/whoami", headers={"X-User-Id": "from-header"})
        assert r.json()["user_id"] == "from-header"

    def test_no_cookie_no_header_returns_empty(self):
        """Anônimo: user_id_var fica vazio (não bloqueia request)."""
        client = TestClient(self._make_app())
        r = client.get("/whoami")
        assert r.json()["user_id"] == ""

    def test_legacy_session_cookie_no_longer_consumed(self):
        """Defensivo contra regressão: o cookie antigo `session` (que o
        projeto nunca emitiu) NÃO deve mais ser consumido. Era a fonte do
        bug — gerava strings 'sess:abcd...' inúteis no log.
        """
        client = TestClient(self._make_app())
        client.cookies.set("session", "abcd1234deadbeef")
        r = client.get("/whoami")
        # Nada de `sess:` no resultado — só vazio porque não há cookie `user_id`.
        assert r.json()["user_id"] == ""
        assert "sess:" not in r.json()["user_id"]

    def test_long_cookie_value_truncated_to_64_chars(self):
        """Defesa contra envenenamento via cookie inflado (uid verificado é truncado)."""
        from app.core.auth import sign_session
        client = TestClient(self._make_app())
        long_uid = "x" * 200
        client.cookies.set("user_id", sign_session(long_uid))
        r = client.get("/whoami")
        assert len(r.json()["user_id"]) == 64


# ─── Retenção uniforme 7d (decisão operacional 2026-05-27) ────────
# Trava o contrato pra ambos os arquivos. Se mudar um sem o outro, UI mostra
# valor diferente do real (TimedRotatingFileHandler.backupCount).


class TestLogRetentionPolicy:
    def test_all_log_files_retention_7_days(self):
        from app.core.logging_setup import _LOG_FILES
        for name, cfg in _LOG_FILES.items():
            assert cfg["retention_days"] == 7, (
                f"_LOG_FILES['{name}'].retention_days = {cfg['retention_days']}; "
                "esperado 7d (decisão 2026-05-27)"
            )

    def test_logs_admin_meta_matches_setup(self):
        """Os dois dicts (logging_setup + logs_admin) devem ter a MESMA
        retenção por arquivo — UI lê de logs_admin, handler real lê de
        logging_setup. Drift = bug silencioso."""
        from app.core.logging_setup import _LOG_FILES
        from app.routes.logs_admin import _LOG_FILES_META
        for name in _LOG_FILES:
            assert name in _LOG_FILES_META, f"'{name}' falta em _LOG_FILES_META"
            assert _LOG_FILES[name]["retention_days"] == _LOG_FILES_META[name]["retention_days"], (
                f"retention_days divergente para '{name}': "
                f"setup={_LOG_FILES[name]['retention_days']}, "
                f"meta={_LOG_FILES_META[name]['retention_days']}"
            )
