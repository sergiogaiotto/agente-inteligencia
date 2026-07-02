"""Helpers de datetime para colunas TIMESTAMP (sem time zone) do schema.

Convenção do projeto: todo timestamp persistido é UTC *naive*. As colunas são
TIMESTAMP (não TIMESTAMP WITH TIME ZONE) e o asyncpg rejeita datetime tz-aware
nesses binds ("can't subtract offset-naive and offset-aware datetimes").
`datetime.now()` também é proibido em writes: grava hora LOCAL do container e
mistura fusos na mesma tabela (incidente: interactions com ended_at 3h atrás
de started_at).
"""
from __future__ import annotations

from datetime import datetime, timezone


def naive_utc_now() -> datetime:
    """UTC corrente como datetime tz-naive — único formato aceito em writes
    de colunas TIMESTAMP. Preserva o instante UTC e remove o tzinfo."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
