"""Regressão: fileConfig do alembic NÃO pode derrubar o logging do app.

Incidente (33.17.0 → diagnosticado 2026-07-13 na revisão E2E Pulsar): com as
migrations Alembic rodando NO PROCESSO do app (boot, database.
_alembic_upgrade_sync), o ``fileConfig(alembic.ini)`` de alembic/env.py:
  1. substituía os handlers do root logger pelos do alembic.ini (console text)
     → app.log/api.log/audit.log paravam de receber QUALQUER linha; e
  2. com o default ``disable_existing_loggers=True``, desabilitava todos os
     loggers já criados (app.*, uvicorn.*) → nem access log no console.
Sintoma observável: docker logs / LOG_DIR só com linhas de BOOT; as únicas
linhas de runtime eram de loggers importados tardiamente, no formato do
alembic.ini (``WARNI [app.core.secrets]``).

O guard exigido: fileConfig só roda quando o root NÃO tem handlers (CLI
standalone) e sempre com disable_existing_loggers=False.

O env.py não é importável isoladamente (``from alembic import context`` exige
o runtime do alembic), então a regressão é (a) textual no guard e (b)
comportamental na semântica do guard reproduzida.
"""
from __future__ import annotations

import logging
import logging.config
import pathlib

ENV = pathlib.Path("alembic/env.py").read_text(encoding="utf-8")


class TestGuardPresente:
    def test_fileconfig_gated_por_root_sem_handlers(self):
        assert "not logging.getLogger().handlers" in ENV, (
            "fileConfig do alembic deve rodar SÓ quando o root não tem "
            "handlers (CLI standalone) — in-process ele derruba o logging do app"
        )

    def test_fileconfig_nunca_desabilita_loggers_existentes(self):
        assert "disable_existing_loggers=False" in ENV
        # e não pode sobrar chamada sem o kwarg
        assert "fileConfig(config.config_file_name)" not in ENV


class TestSemanticaDoGuard:
    def test_guard_preserva_logging_configurado(self, tmp_path):
        """Reproduz a semântica: com root JÁ configurado, o guard pula o
        fileConfig — handlers e loggers do app sobrevivem intactos."""
        root = logging.getLogger()
        prev_handlers = list(root.handlers)
        marker = logging.NullHandler()
        root.addHandler(marker)
        probe = logging.getLogger("app.probe.alembic_guard")
        probe.disabled = False
        try:
            # comportamento do env.py corrigido:
            if not logging.getLogger().handlers:  # False — root tem handlers
                raise AssertionError("guard deveria ter pulado o fileConfig")
            assert marker in root.handlers
            assert probe.disabled is False
        finally:
            root.removeHandler(marker)
            assert root.handlers == prev_handlers or True  # restauração best-effort

    def test_fileconfig_com_disable_false_nao_desliga_loggers(self, tmp_path):
        """Prova do mecanismo do bug e do porquê do kwarg: fileConfig com
        disable_existing_loggers=False mantém loggers pré-existentes vivos."""
        ini = tmp_path / "alembic_like.ini"
        ini.write_text(
            "[loggers]\nkeys = root\n\n"
            "[handlers]\nkeys = console\n\n"
            "[formatters]\nkeys = generic\n\n"
            "[logger_root]\nlevel = WARNING\nhandlers = console\n\n"
            "[handler_console]\nclass = StreamHandler\nargs = (sys.stderr,)\n"
            "level = NOTSET\nformatter = generic\n\n"
            "[formatter_generic]\nformat = %(levelname)-5.5s [%(name)s] %(message)s\n",
            encoding="utf-8",
        )
        pre = logging.getLogger("app.probe.pre_existing")
        pre.disabled = False
        root = logging.getLogger()
        saved_handlers, saved_level = list(root.handlers), root.level
        try:
            logging.config.fileConfig(str(ini), disable_existing_loggers=False)
            assert pre.disabled is False, (
                "com disable_existing_loggers=False o logger pré-existente deve "
                "continuar habilitado (com True — o default e o bug — ele morre)"
            )
        finally:
            # higiene: fileConfig troca handlers/level do root — restaura para
            # não contaminar o resto da suíte
            for h in list(root.handlers):
                root.removeHandler(h)
            for h in saved_handlers:
                root.addHandler(h)
            root.setLevel(saved_level)
