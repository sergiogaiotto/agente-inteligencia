"""Setup do Alembic (Onda 4, 33.6.0) — parte unit (sem DB).

Valida que a config carrega, que a revisão baseline é o head e é no-op. O
comportamento real (upgrade carimba a alembic_version) vive em
tests/integration/test_alembic_real_postgres.py (Postgres real).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def test_alembic_config_e_baseline_e_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg)

    assert script.get_heads() == ["0001_baseline"]
    base = script.get_revision("0001_baseline")
    assert base.down_revision is None


def test_baseline_e_no_op():
    p = _ROOT / "alembic" / "versions" / "0001_baseline.py"
    spec = importlib.util.spec_from_file_location("_baseline_probe", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod.revision == "0001_baseline"
    assert mod.down_revision is None
    assert mod.upgrade() is None   # no-op (schema vem do init_db DDL)
    assert mod.downgrade() is None
