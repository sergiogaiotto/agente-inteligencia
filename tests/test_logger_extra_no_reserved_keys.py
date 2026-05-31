"""Regressão + guard-rail: logger.<level>(extra={...}) não pode usar chaves
reservadas do LogRecord.

# Bug original (PR #225)

POST /knowledge-sources/{ks_id}/promote-to-table → 500 com:

    KeyError: "Attempt to overwrite 'name' in LogRecord"

em `app/evidence/tabular.py:939`. O `extra={...}` do `_tabular_logger.info`
tinha a chave `"name": display_name`. Python logging.makeRecord() (em
logging/__init__.py:1606) levanta KeyError ao detectar colisão com
atributos do LogRecord.

# Atributos reservados (Python 3.11)

https://docs.python.org/3/library/logging.html#logrecord-attributes

# Estratégia deste arquivo

1. Smoke do logger corrigido com payload realista (não colide).
2. Sanity que confirma que o Python continua rejeitando keys reservadas
   (protege a premissa do guard-rail caso runtime mude).
3. **Guard-rail AST-based**: varre `app/**/*.py` por chamadas
   `logger.info/warning/error/critical/debug/exception(..., extra={...})`
   e falha se algum dict literal usa key reservada. Pega bugs latentes
   do mesmo tipo em outros lugares do código sem rodar a aplicação.
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest


# Atributos reservados de LogRecord (Python 3.11). Se Python mudar isso,
# atualizar aqui. Fonte: cpython/Lib/logging/__init__.py — _logRecordReservedAttrs.
RESERVED_LOGRECORD_KEYS = frozenset({
    "name", "msg", "args", "levelname", "levelno",
    "pathname", "filename", "module",
    "exc_info", "exc_text", "stack_info",
    "lineno", "funcName",
    "created", "msecs", "relativeCreated",
    "thread", "threadName", "processName", "process",
    "message",
})

# Nomes dos métodos de logger que aceitam `extra=` como kwarg.
LOGGER_METHODS = frozenset({
    "debug", "info", "warning", "warn", "error", "critical",
    "exception", "log",
})


# ─── Regressão direta do bug do PR #225 ────────────────────────


class TestPromoteLoggerRegression:
    def test_tabular_promote_completed_uses_table_name_not_name(self):
        """O bloco `promote_completed` em tabular.py não pode mais usar
        a chave 'name' no extra do logger."""
        src = Path("app/evidence/tabular.py").read_text(encoding="utf-8")
        idx = src.find("promote_completed")
        assert idx >= 0, "promote_completed event não encontrado em tabular.py"
        snippet = src[idx:idx + 1500]

        # Falso-positivo prevenido: 'name' como sufixo (table_name, sheet_name)
        # não bate o regex porque exige aspas + dois-pontos imediatamente depois.
        import re
        pattern = re.compile(r'[\'"]name[\'"]\s*:')
        m = pattern.search(snippet)
        assert m is None, (
            "Trecho de promote_completed ainda usa key reservada 'name' no extra. "
            f"Renomeie para 'table_name' ou similar. Snippet: "
            f"{snippet[max(0, m.start()-40):m.start()+80]}"
        )

    def test_tabular_logger_smoke_with_safe_payload_does_not_raise(self):
        """Smoke: chama _tabular_logger.info com payload equivalente ao do
        código corrigido — não pode levantar KeyError."""
        from app.evidence.tabular import _tabular_logger
        try:
            _tabular_logger.info(
                "promote_completed",
                extra={
                    "event": "tabular.promote.completed",
                    "ks_id": "ks-x", "table_id": "tbl-x", "urn": "urn:x",
                    "table_name": "minha_tabela", "sheet_name": "Sheet1",
                    "rows": 10, "columns": 5, "size_bytes": 1024,
                    "quality_score": 0.95, "suggested_pk": "id",
                    "duration_ms": 12.3,
                },
            )
        except KeyError as e:
            pytest.fail(f"Logger seguro levantou KeyError: {e}")

    def test_logger_with_reserved_name_key_still_collides(self):
        """Sanity: protege a premissa do guard-rail.

        Se o Python um dia parar de bloquear keys reservadas no extra,
        este teste falha e nos avisa que o guard-rail virou letra morta.

        Detalhe: chamamos `makeRecord()` DIRETO em vez de `logger.info()`
        porque o último é early-returned quando o logger não tem handler
        configurado (root level default = WARNING, INFO é skipped antes
        de chegar em makeRecord). Em produção há handlers; aqui em test
        não — isso explicaria um falso "passou" sem o fix.
        """
        logger = logging.getLogger("test.collision.sanity")
        with pytest.raises(KeyError, match="name"):
            logger.makeRecord(
                logger.name, logging.INFO, "fn", 1, "msg", (),
                exc_info=None, extra={"name": "should_collide"},
            )


# ─── Guard-rail amplo: AST-based, varre todo app/ ─────────────


class TestNoReservedLogRecordKeysInExtra:
    """AST scan: cada chamada `<...>.<level>(extra={...})` em app/**/*.py
    onde extra é dict literal não pode usar key reservada de LogRecord."""

    def _scan(self) -> list[tuple[str, int, str, str]]:
        """Retorna [(path, line, method, reserved_key)] para cada problema."""
        problems: list[tuple[str, int, str, str]] = []
        for py_path in Path("app").rglob("*.py"):
            try:
                tree = ast.parse(py_path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not isinstance(func, ast.Attribute):
                    continue
                method = func.attr
                if method not in LOGGER_METHODS:
                    continue
                for kw in node.keywords:
                    if kw.arg != "extra":
                        continue
                    # Só checa dict literal — se for variável, não dá pra inspecionar
                    if not isinstance(kw.value, ast.Dict):
                        continue
                    for key_node in kw.value.keys:
                        if (
                            isinstance(key_node, ast.Constant)
                            and isinstance(key_node.value, str)
                            and key_node.value in RESERVED_LOGRECORD_KEYS
                        ):
                            problems.append((
                                str(py_path),
                                key_node.lineno,
                                method,
                                key_node.value,
                            ))
        return problems

    def test_no_logger_call_uses_reserved_key_in_extra(self):
        """Falha se qualquer arquivo em app/ tem `logger.X(..., extra={"<reserved>": ...})`."""
        problems = self._scan()
        formatted = "\n".join(
            f"  {p}:{ln} → .{method}(extra={{...'{k}'...}})"
            for p, ln, method, k in problems
        )
        assert not problems, (
            "Chamada(s) de logger com chave reservada do LogRecord em `extra=`. "
            f"Renomeie (ex.: 'name' → 'table_name'; 'message' → 'event_msg').\n"
            f"Reservadas: {sorted(RESERVED_LOGRECORD_KEYS)}\n"
            f"Encontrado(s):\n{formatted}"
        )

    def test_scanner_actually_detects_synthetic_offender(self, tmp_path, monkeypatch):
        """Sanity do scanner: se inventarmos um arquivo offensivo em runtime,
        a varredura deve pegá-lo. Defesa contra "regex que nunca encontra nada"."""
        fake = tmp_path / "fake_app"
        fake.mkdir()
        (fake / "bad.py").write_text(
            "import logging\n"
            "logger = logging.getLogger('x')\n"
            "logger.info('hi', extra={'name': 'oops', 'event': 'y'})\n",
            encoding="utf-8",
        )

        # Roda o scanner contra a pasta sintética usando a mesma lógica
        problems = []
        for py in fake.rglob("*.py"):
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                if not isinstance(f, ast.Attribute) or f.attr not in LOGGER_METHODS:
                    continue
                for kw in node.keywords:
                    if kw.arg != "extra" or not isinstance(kw.value, ast.Dict):
                        continue
                    for k in kw.value.keys:
                        if (
                            isinstance(k, ast.Constant)
                            and isinstance(k.value, str)
                            and k.value in RESERVED_LOGRECORD_KEYS
                        ):
                            problems.append((str(py), k.lineno, k.value))

        assert problems, (
            "Scanner não detectou logger.info(extra={'name': ...}) sintético — "
            "guard-rail está cego. Algo errado na lógica de varredura."
        )
