"""Logging estruturado para troubleshooting e observabilidade.

Arquitetura:
- **JSONL** (1 JSON por linha) para todos os logs em arquivo — Loki parseia
  nativamente via `| json` no LogQL; Grafana consume em tempo real.
- **5 arquivos** rotacionados em `logs/` (configurável via env):
    - app.log     — geral, INFO+
    - tabular.log — eventos da Onda Tabular (analyze/promote/append/query)
    - api.log     — request/response REST com latency
    - audit.log   — writes em DB (auditoria de mudanças)
    - errors.log  — só ERROR + CRITICAL (escalation rápida)
- **TimedRotatingFileHandler** (diário, retenções diferentes por arquivo).
- **Catálogo de eventos canônicos** (ver app/data_tables/events.py): cada
  log tem campo `event` consultável via LogQL (`| json | event="x"`).
- **Context vars** (request_id, trace_id, user_id) injetados automaticamente
  pelo JsonFormatter — populados pelo middleware HTTP.
- **PII redaction**: campos sensíveis (password, token, api_key, secret)
  são redactados antes de serializar.

ENV vars:
- LOG_DIR:    pasta dos arquivos (default 'logs')
- LOG_LEVEL:  default 'INFO'
- LOG_FORMAT: 'json' (default em prod) ou 'text' (default em dev/test)
- LOG_FILE_ENABLED: '1' (default) ou '0' (desliga handlers de arquivo)
- LOG_CONSOLE_ENABLED: '1' (default) ou '0'

Uso:
    from app.core.logging_setup import setup_logging
    setup_logging()  # idempotente, chama no lifespan

    import logging
    logger = logging.getLogger("tabular.promote")
    logger.info("table_created", extra={"event": "tabular.promote.completed",
                                       "table_id": "t-x", "rows": 1234})
"""
from __future__ import annotations

import json
import logging
import os
import sys
import traceback
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

# Context vars populados pelo RequestContextMiddleware (Fase 2).
# Mantidos aqui (não em request_context.py) para evitar import circular
# quando JsonFormatter precisa ler — formatter roda em qualquer logger.
import contextvars

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default=""
)
trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "trace_id", default=""
)
user_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "user_id", default=""
)


# ─── PII redaction ────────────────────────────────────────────────


_SENSITIVE_KEYS = frozenset({
    "password", "passwd", "secret", "token", "api_key", "apikey",
    "authorization", "auth", "x-api-key", "private_key", "access_token",
    "refresh_token", "bearer", "client_secret",
})


def _redact_value(v: Any) -> Any:
    """Redação rasa: se valor é dict/list, recursa; senão mantém."""
    if isinstance(v, dict):
        return _redact_dict(v)
    if isinstance(v, list):
        return [_redact_value(x) for x in v]
    return v


def _redact_dict(d: dict) -> dict:
    """Redaciona valores de chaves sensíveis em um dict (recursivo)."""
    out = {}
    for k, v in d.items():
        if isinstance(k, str) and k.lower() in _SENSITIVE_KEYS:
            out[k] = "***REDACTED***"
        else:
            out[k] = _redact_value(v)
    return out


# ─── JSON Formatter ──────────────────────────────────────────────


# Atributos padrão do LogRecord que não devem ir para o "extras" (já estão
# no top-level do JSON output ou são internos do logging).
_STANDARD_LOGRECORD_ATTRS = frozenset({
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "message", "module",
    "msecs", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "thread", "threadName",
    "taskName",  # Python 3.12+
})


class JsonFormatter(logging.Formatter):
    """Formatter que serializa LogRecord como JSON em uma linha (JSONL).

    Campos sempre presentes:
        ts, level, logger, msg

    Context vars (se setados pelo middleware):
        request_id, trace_id, user_id

    Campos extras (passados via `logger.info(..., extra={...})`):
        qualquer key do extra vira top-level no JSON.

    Stacktrace de exceção:
        exception: {type, message, traceback}
    """

    def __init__(self, *, include_context: bool = True):
        super().__init__()
        self.include_context = include_context

    def format(self, record: logging.LogRecord) -> str:
        # Timestamp ISO 8601 UTC com milissegundos. record.created é POSIX
        # timestamp (float). Construímos manualmente porque strftime %f
        # não funciona em todas as plataformas (Windows).
        import datetime as _dt
        dt = _dt.datetime.utcfromtimestamp(record.created)
        ts = dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{int(record.msecs):03d}Z"
        payload: dict[str, Any] = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        # Context vars (request lifecycle)
        if self.include_context:
            rid = request_id_var.get()
            tid = trace_id_var.get()
            uid = user_id_var.get()
            if rid:
                payload["request_id"] = rid
            if tid:
                payload["trace_id"] = tid
            if uid:
                payload["user_id"] = uid

        # Extras do logger.x(..., extra={...})
        for key, value in record.__dict__.items():
            if key in _STANDARD_LOGRECORD_ATTRS:
                continue
            if key.startswith("_"):
                continue
            try:
                # Redaciona PII se for dict
                if isinstance(value, dict):
                    payload[key] = _redact_dict(value)
                else:
                    payload[key] = value
            except Exception:
                payload[key] = repr(value)

        # Exception traceback
        if record.exc_info:
            exc_type, exc_val, exc_tb = record.exc_info
            payload["exception"] = {
                "type": exc_type.__name__ if exc_type else "?",
                "message": str(exc_val) if exc_val else "",
                "traceback": "".join(traceback.format_exception(exc_type, exc_val, exc_tb)),
            }

        try:
            return json.dumps(payload, ensure_ascii=False, default=str)
        except Exception as e:
            # Fallback: erro de serialização não pode derrubar o app
            return json.dumps({
                "ts": payload.get("ts"),
                "level": "ERROR",
                "logger": "logging_setup",
                "msg": f"JsonFormatter failed: {e}",
                "original_msg": record.getMessage(),
            })


class TextFormatter(logging.Formatter):
    """Formatter texto legível para dev/console. Inclui request_id se houver."""

    def __init__(self):
        super().__init__(
            fmt="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        rid = request_id_var.get()
        if rid:
            base = f"[{rid[:8]}] {base}"
        # Inclui campos extras como sufixo k=v (cap 200 chars por linha)
        extras = {
            k: v for k, v in record.__dict__.items()
            if k not in _STANDARD_LOGRECORD_ATTRS and not k.startswith("_")
        }
        if extras:
            kv = " ".join(f"{k}={v}" for k, v in list(extras.items())[:6])
            base += f"  {kv[:200]}"
        return base


# ─── Setup ───────────────────────────────────────────────────────


_LOG_FILES = {
    # Retenção uniforme: 7 dias em todos. Decisão operacional 2026-05-27 — disco
    # do app é compartilhado com uploads/evidências (volume /app/data), e logs
    # antigos não ajudam troubleshoot (recorremos a Grafana/Loki pra análise
    # longa). Atenção a `audit.log`: vinha com 90d por convenção de compliance
    # — se sua política exigir > 7d, sobrescreva via env LOG_RETENTION_AUDIT_DAYS
    # ou exporte audit_log do DB (single source of truth via tabela `audit_log`).
    "app":     {"level": logging.INFO,    "retention_days": 7,
                "loggers": ["app", "uvicorn", "fastapi"]},
    "tabular": {"level": logging.DEBUG,   "retention_days": 7,
                "loggers": ["tabular"]},
    "api":     {"level": logging.INFO,    "retention_days": 7,
                "loggers": ["app.api"]},
    "audit":   {"level": logging.INFO,    "retention_days": 7,
                "loggers": ["audit"]},
    "errors":  {"level": logging.ERROR,   "retention_days": 7,
                "loggers": None},  # captura de qualquer logger
}


def _truthy(env_val: str | None) -> bool:
    return (env_val or "").strip().lower() in ("1", "true", "yes", "on")


def _make_rotating_handler(path: Path, level: int, retention_days: int,
                          formatter: logging.Formatter) -> TimedRotatingFileHandler:
    """TimedRotatingFileHandler diário com retenção custom."""
    h = TimedRotatingFileHandler(
        path, when="midnight", interval=1,
        backupCount=retention_days, encoding="utf-8", utc=True,
    )
    h.setLevel(level)
    h.setFormatter(formatter)
    h.suffix = "%Y-%m-%d"
    return h


class _LoggerFilter(logging.Filter):
    """Filtro que aceita só logs dos loggers cujo nome começa com algum prefix."""

    def __init__(self, prefixes: list[str]):
        super().__init__()
        self.prefixes = tuple(prefixes)

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.startswith(self.prefixes)


_setup_done = False


def setup_logging(*, force: bool = False) -> dict:
    """Configura o logging da aplicação. Idempotente.

    Lê config do env:
    - LOG_DIR (default 'logs')
    - LOG_LEVEL (default 'INFO')
    - LOG_FORMAT ('json' default em prod; 'text' default sob PYTEST_CURRENT_TEST)
    - LOG_FILE_ENABLED (default '1')
    - LOG_CONSOLE_ENABLED (default '1')

    Returns:
        Dict com info do setup (dir, handlers ativos) — útil pra debug.
    """
    global _setup_done
    if _setup_done and not force:
        return {"already_setup": True}

    log_dir = Path(os.environ.get("LOG_DIR", "logs"))
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    is_test = bool(os.environ.get("PYTEST_CURRENT_TEST"))
    default_format = "text" if is_test else "json"
    log_format = os.environ.get("LOG_FORMAT", default_format).lower()
    file_enabled = _truthy(os.environ.get("LOG_FILE_ENABLED", "1"))
    console_enabled = _truthy(os.environ.get("LOG_CONSOLE_ENABLED", "1"))

    # Em testes, default: SÓ console, sem arquivos (evita poluir CI)
    if is_test and "LOG_FILE_ENABLED" not in os.environ:
        file_enabled = False

    # Cria pasta se file logging ativo
    if file_enabled:
        log_dir.mkdir(parents=True, exist_ok=True)

    # Limpa handlers anteriores do root + nossos loggers nomeados
    # (idempotência ao chamar setup_logging() múltiplas vezes)
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    # Define nível raiz
    root.setLevel(getattr(logging, log_level, logging.INFO))

    # Formatter
    json_fmt = JsonFormatter()
    text_fmt = TextFormatter()
    chosen_fmt = json_fmt if log_format == "json" else text_fmt

    active_handlers = []

    # Console (stdout) — sempre se habilitado
    if console_enabled:
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(root.level)
        console.setFormatter(chosen_fmt)
        root.addHandler(console)
        active_handlers.append("console")

    # File handlers
    if file_enabled:
        for name, cfg in _LOG_FILES.items():
            path = log_dir / f"{name}.log"
            h = _make_rotating_handler(
                path, cfg["level"], cfg["retention_days"], json_fmt,
            )
            # Filtro: só accepta logs dos prefixos configurados
            if cfg["loggers"] is not None:
                h.addFilter(_LoggerFilter(cfg["loggers"]))
            root.addHandler(h)
            active_handlers.append(name)

    _setup_done = True
    info = {
        "log_dir": str(log_dir.resolve()) if file_enabled else None,
        "log_level": log_level,
        "log_format": log_format,
        "file_enabled": file_enabled,
        "console_enabled": console_enabled,
        "handlers": active_handlers,
        "test_mode": is_test,
    }
    # Use root logger pra registrar config (vai pro console pelo menos)
    logging.getLogger("app.core.logging_setup").info(
        "logging_configured", extra={"event": "logging.configured", **info}
    )
    return info


def get_audit_logger() -> logging.Logger:
    """Atalho para o logger de auditoria (escreve em audit.log)."""
    return logging.getLogger("audit")


def get_tabular_logger(sub: str = "") -> logging.Logger:
    """Atalho para loggers da Onda Tabular. Ex: get_tabular_logger('promote')
    → 'tabular.promote' (escreve em tabular.log)."""
    name = f"tabular.{sub}" if sub else "tabular"
    return logging.getLogger(name)


def get_api_logger() -> logging.Logger:
    """Atalho para o logger HTTP (escreve em api.log)."""
    return logging.getLogger("app.api")
