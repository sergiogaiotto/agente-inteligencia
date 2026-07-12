"""Coerção de write do Repository (33.6.1) — parte unit (lógica pura).

Blinda os footguns asyncpg que os mocks escondem: dict/list → json.dumps em
colunas json/jsonb; datetime tz-aware → naive UTC em colunas TIMESTAMP. O
end-to-end (persiste contra Postgres real) vive em
tests/integration/test_repository_coercion_real_postgres.py.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from app.core.database import Repository
from app.core.datetime_utils import to_naive_utc


class TestCoerceValue:
    def test_dict_em_jsonb_vira_json_string(self):
        v = Repository._coerce_value({"a": [1, 2], "b": "x"}, "jsonb")
        assert isinstance(v, str)
        assert json.loads(v) == {"a": [1, 2], "b": "x"}

    def test_list_em_json_vira_json_string(self):
        v = Repository._coerce_value([1, {"k": "v"}], "json")
        assert isinstance(v, str)
        assert json.loads(v) == [1, {"k": "v"}]

    def test_string_ja_serializada_passa_intacta(self):
        # caller que já fez json.dumps NÃO é dupla-serializado.
        v = Repository._coerce_value('{"a":1}', "jsonb")
        assert v == '{"a":1}'

    def test_datetime_aninhado_em_jsonb_nao_quebra(self):
        # default=str serializa datetimes aninhados em vez de estourar.
        d = {"when": datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)}
        v = Repository._coerce_value(d, "jsonb")
        assert isinstance(v, str) and "2026-01-01" in v

    def test_datetime_aware_em_timestamp_vira_naive(self):
        aware = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        v = Repository._coerce_value(aware, "timestamp without time zone")
        assert isinstance(v, datetime) and v.tzinfo is None

    def test_datetime_naive_em_timestamp_passa_intacto(self):
        naive = datetime(2026, 1, 1, 12, 0)
        v = Repository._coerce_value(naive, "timestamp without time zone")
        assert v is naive

    def test_coluna_text_nao_coage_dict(self):
        # só json/jsonb coage dict; TEXT (ex.: interactions.metadata) passa cru
        # (o caller já serializa como sempre).
        d = {"a": 1}
        v = Repository._coerce_value(d, "text")
        assert v is d

    def test_none_passa_intacto(self):
        assert Repository._coerce_value(None, "jsonb") is None
        assert Repository._coerce_value(None, "timestamp without time zone") is None


class TestToNaiveUtc:
    def test_aware_vira_naive_utc(self):
        from datetime import timedelta
        # 09:00 em UTC-3 == 12:00 UTC naive
        tz = timezone(timedelta(hours=-3))
        aware = datetime(2026, 1, 1, 9, 0, tzinfo=tz)
        out = to_naive_utc(aware)
        assert out.tzinfo is None
        assert out == datetime(2026, 1, 1, 12, 0)

    def test_naive_passa_intacto(self):
        naive = datetime(2026, 1, 1, 12, 0)
        assert to_naive_utc(naive) is naive
