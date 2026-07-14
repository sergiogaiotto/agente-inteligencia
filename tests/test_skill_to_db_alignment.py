"""Regressão pro bug 'data_tables column not exists' (Onda Tabular).

Garante que TODAS as chaves devolvidas por `skill_to_db_dict()` (mais 'id'
e 'tags' que o handler de rota adiciona) batem com colunas reais da DDL
da tabela `skills` em app/core/database.py.

Bug histórico: PR #110/#115 (Onda Tabular) adicionou `data_tables` no parser
e no dict de DB, mas esqueceu de adicionar coluna na tabela skills E
migration idempotente correspondente. Resultado: criar skill estourava 500
com `UndefinedColumnError`. Este teste pega o gap pra trás (se voltar a
acontecer com algum campo novo) sem precisar de Postgres real.
"""
from __future__ import annotations

import re
from pathlib import Path


from app.skill_parser.parser import ParsedSkill, SkillFrontmatter, skill_to_db_dict


# Caminho fixo (relativo ao repo root) — assume layout estável.
_DATABASE_PY = Path(__file__).resolve().parent.parent / "app" / "core" / "database.py"


def _extract_skills_table_columns() -> set[str]:
    """Lê app/core/database.py e extrai os nomes de coluna da tabela skills.

    Cobre 2 fontes:
    1. DDL inline: CREATE TABLE IF NOT EXISTS skills (...)
    2. Migrations idempotentes: ALTER TABLE skills ADD COLUMN IF NOT EXISTS <nome>

    Retorna set de nomes em lowercase. Reservadas/sintaxe (CHECK, PRIMARY KEY,
    REFERENCES) são ignoradas — só pega identificadores válidos como col name.
    """
    src = _DATABASE_PY.read_text(encoding="utf-8")

    # 1. CREATE TABLE skills (...)
    m = re.search(
        r"CREATE TABLE IF NOT EXISTS skills\s*\((.*?)\);",
        src,
        re.DOTALL | re.IGNORECASE,
    )
    cols: set[str] = set()
    if m:
        body = m.group(1)
        for line in body.splitlines():
            line = line.strip().rstrip(",")
            if not line or line.startswith("--"):
                continue
            # Pula constraints e references
            upper = line.upper()
            if upper.startswith(("CHECK", "PRIMARY KEY", "FOREIGN KEY", "REFERENCES",
                                  "UNIQUE (", "CONSTRAINT")):
                continue
            # Primeira "palavra" é o nome da coluna
            tok = line.split()[0].strip('"').strip("`")
            if tok and tok.isidentifier():
                cols.add(tok.lower())

    # 2. ALTER TABLE skills ADD COLUMN [IF NOT EXISTS] <nome>
    for m2 in re.finditer(
        r"ALTER TABLE skills ADD COLUMN(?:\s+IF NOT EXISTS)?\s+([a-zA-Z_][a-zA-Z0-9_]*)",
        src,
        re.IGNORECASE,
    ):
        cols.add(m2.group(1).lower())

    return cols


def _all_skill_to_db_keys() -> set[str]:
    """Constrói um ParsedSkill default e extrai todas as chaves do dict."""
    parsed = ParsedSkill(frontmatter=SkillFrontmatter(id="urn:skill:x:subagent:y"))
    db = skill_to_db_dict(parsed)
    return set(db.keys())


class TestSkillToDbAlignment:
    def test_all_dict_keys_exist_as_columns(self):
        """Cada chave que skill_to_db_dict gera DEVE ter coluna correspondente
        na tabela skills — senão INSERT estoura UndefinedColumnError em prod."""
        dict_keys = _all_skill_to_db_keys()
        table_cols = _extract_skills_table_columns()

        # `id` e `tags` são adicionados pelo handler (não pelo skill_to_db_dict),
        # mas também precisam existir. Adicionamos manualmente pra cobrir.
        all_keys = dict_keys | {"id", "tags"}

        missing = sorted(all_keys - table_cols)
        assert not missing, (
            f"Bug de migration: skill_to_db_dict gera campos sem coluna "
            f"correspondente em `skills`: {missing}. Adicione "
            f"ALTER TABLE skills ADD COLUMN IF NOT EXISTS <name> ... em "
            f"_IDEMPOTENT_MIGRATIONS (app/core/database.py)."
        )

    def test_data_tables_column_exists(self):
        """Regressão específica do bug histórico — Onda Tabular esqueceu
        de adicionar essa coluna, criar skill quebrava com 500."""
        table_cols = _extract_skills_table_columns()
        assert "data_tables" in table_cols, (
            "Coluna `data_tables` ausente na tabela `skills`. Bug histórico "
            "da Onda Tabular: PR #110/#115 adicionou no parser mas esqueceu "
            "da migration."
        )

    def test_extractor_finds_known_baseline_columns(self):
        """Sanity do extrator de colunas — se quebrar a DDL ou o regex, pegar
        cedo. Lista de colunas estáveis que existem há várias ondas."""
        table_cols = _extract_skills_table_columns()
        baseline = {"id", "urn", "name", "kind", "purpose", "workflow",
                    "raw_content", "content_hash", "tags"}
        missing_baseline = baseline - table_cols
        assert not missing_baseline, (
            f"Extrator não achou colunas baseline (provável regex quebrado): "
            f"{missing_baseline}"
        )
