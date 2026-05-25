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
