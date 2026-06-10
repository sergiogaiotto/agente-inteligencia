"""Catálogo de Dados — service de curadoria (Onda Catálogo).

`apply_catalog` é a ÚNICA escrita do catálogo (DNA: IA sugere, HUMANO cura,
metadata curada é determinística). A geração por IA (sugestão volátil) vem no
PR3 e NÃO persiste — só este caminho grava.

Valida nomes ∈ schema_json VIVO e pii_category ∈ enum FECHADO, monta o
catalog_json indexado por NOME com proveniência 'human', e grava com
json.dumps ANTES do asyncpg (armadilha JSONB+asyncpg: Repository.update passa o
valor cru → dict em coluna JSONB quebra com "expected str, got dict").
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Awaitable, Callable

from app.core.database import data_tables_repo
from app.data_tables.queries import find_by_id_with_ks
from app.data_tables.types import (
    PiiCategory,
    normalize_output_treatment,
    normalize_pii_category,
)
from app.evidence.tabular import TabularError


async def apply_catalog(
    row: dict,
    table_description: str,
    columns: list[dict],
    user: dict,
) -> dict:
    """Persiste a curadoria HUMANA do catálogo de uma data_table.

    `row` vem de find_by_id_with_ks (já autorizado por visibility no caller).
    Cada coluna é validada contra o schema VIVO (nome inexistente → 400) e a
    pii_category contra o enum fechado (inválida → 400; aqui REJEITA, diferente
    da sugestão da IA que coage p/ 'none'). Retorna o row reconciliado atualizado.
    """
    table_id = row.get("id")
    schema = row.get("schema_json") or []
    schema_names = {c.get("name") for c in schema if isinstance(c, dict)}

    now_iso = datetime.utcnow().isoformat()
    uid = (user or {}).get("id") or ""

    cat_columns: dict[str, Any] = {}
    for c in (columns or []):
        name = str(c.get("name") or "").strip()
        if name not in schema_names:
            raise TabularError(
                f"Coluna '{name}' não existe no schema da tabela.", status_code=400
            )
        pii_raw = c.get("pii_category", "none")
        try:
            pii = PiiCategory(str(pii_raw).strip().lower()).value
        except ValueError:
            raise TabularError(
                f"pii_category inválida: '{pii_raw}'. Aceitas: "
                f"{[p.value for p in PiiCategory]}.",
                status_code=400,
            )
        entry = {
            "description": str(c.get("description") or "").strip(),
            "pii_category": pii,
            "source": "human",
            "curated_by": uid,
            "curated_at": now_iso,
        }
        # Tratamento de saída (Exibir/Mascarar/Suprimir) — só persiste quando
        # explicitamente setado e válido; ausente → herda o default da categoria.
        treat = normalize_output_treatment(c.get("output_treatment"))
        if treat is not None:
            entry["output_treatment"] = treat
        cat_columns[name] = entry

    catalog_json = {
        "version": 1,
        "table": {
            "description_source": "human",
            "curated_by": uid,
            "curated_at": now_iso,
        },
        "columns": cat_columns,
    }

    await data_tables_repo.update(table_id, {
        # JSONB: dumps ANTES do asyncpg (Repository.update passa o valor cru).
        "catalog_json": json.dumps(catalog_json, ensure_ascii=False),
        "description": str(table_description or "").strip(),
        "updated_at": datetime.utcnow(),
    })
    return await find_by_id_with_ks(table_id)


# ─── Sugestão por IA (PR3) — IA SUGERE, NÃO persiste ──────────────


def build_suggestion_messages(table_name: str, schema: Any, sample_rows: list) -> list[dict]:
    """Monta as messages do LLM para sugerir o catálogo. Função PURA.

    A amostra é apenas dica (omitida pelo caller p/ bases sensíveis). O LLM é
    instruído a descrever SÓ as colunas listadas e usar o enum fechado de PII.
    """
    cols = "\n".join(
        f"- {c.get('name')}: {c.get('type')} ({'nullable' if c.get('nullable') else 'not null'})"
        for c in (schema if isinstance(schema, list) else []) if isinstance(c, dict)
    )
    sample_block = ""
    if sample_rows:
        dump = json.dumps(sample_rows[:10], ensure_ascii=False, default=str)
        sample_block = f"\n\nAMOSTRA (só dica — NÃO invente valores):\n{dump[:2000]}"
    enum = [p.value for p in PiiCategory]
    system = (
        "Você é um catalogador de dados. Para a tabela e schema dados, gere: (1) uma "
        "descrição CURTA da TABELA; (2) para CADA coluna, uma descrição objetiva (1 frase) "
        f"e a categoria de PII de um enum FECHADO {enum}. REGRAS: descreva APENAS as colunas "
        "listadas, com os NOMES EXATOS; NÃO invente colunas; quando incerto use "
        "pii_category='none' e descrição neutra; a amostra é só dica, NÃO invente valores; "
        "pii_category DEVE pertencer ao enum. Responda APENAS com JSON válido (sem markdown, "
        'sem cercas ```), no formato {"table_description": str, "columns": '
        '[{"name": str, "description": str, "pii_category": str}]}.'
    )
    user = f"Tabela: {table_name}\nColunas:\n{cols}{sample_block}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_suggestion(content: str, schema: Any) -> dict:
    """Parse defensivo do output do LLM + reconciliação por NOME (coração
    anti-alucinação). NUNCA levanta: JSON inválido → sugestão neutra. O LLM
    NUNCA define a lista de colunas — ela vem sempre do schema vivo.
    """
    raw = (content or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    if m:
        raw = m.group(1).strip()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}

    by_name: dict[str, dict] = {}
    llm_cols = data.get("columns")
    if isinstance(llm_cols, list):
        for c in llm_cols:
            if isinstance(c, dict) and c.get("name"):
                by_name[str(c.get("name"))] = c

    columns = []
    for col in (schema if isinstance(schema, list) else []):
        if not isinstance(col, dict):
            continue
        name = col.get("name")
        entry = by_name.get(name)
        entry = entry if isinstance(entry, dict) else {}
        columns.append({
            "name": name,
            "description": str(entry.get("description") or "").strip(),
            "pii_category": normalize_pii_category(entry.get("pii_category")),  # coage (sugestão)
        })
    return {
        "table_description": str(data.get("table_description") or "").strip(),
        "columns": columns,
    }


async def generate_catalog_suggestion(
    row: dict,
    sample_rows: list,
    llm_complete: Callable[[list], Awaitable[str]],
) -> dict:
    """Gera sugestão de catálogo via IA. NÃO persiste (sugestão volátil).

    `llm_complete(messages) -> content` é injetado pelo endpoint (caminho
    resiliente do Wizard). Montagem do prompt e parse/reconciliação são
    determinísticos: a lista de colunas vem do schema, nunca do LLM.
    """
    schema = row.get("schema_json") or []
    messages = build_suggestion_messages(row.get("name") or "", schema, sample_rows or [])
    content = await llm_complete(messages)
    return parse_suggestion(content, schema)
