"""Export/Import do Golden Dataset em CSV (item 5, 52.0.0).

Camada pura (app/core/gold_io.py): template, export round-trip, parser
tolerante (delimitador ;/,, BOM, weight com vírgula, red_flags JSON ou
lista com ;) e erros linha a linha com número FÍSICO da linha.

Rotas: export com filtros (category/split/q pós-fetch), template, e import
em 3 modos (novos/atualizar/concatenar) com DUAS fases — com erro e
apply_partial=false NADA é aplicado (sem meia-importação silenciosa).
Gate root/admin no import (mutação em massa muda baseline/gate de release).
"""
from __future__ import annotations

import json

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import app.routes.dashboard as dash
from app.core.gold_io import (
    GOLD_CSV_COLUMNS, gold_cases_to_csv, parse_gold_csv, template_csv,
)


def _async(value):
    async def _fn(*a, **k):
        return value
    return _fn


def _client():
    app = FastAPI()
    app.include_router(dash.router)
    return TestClient(app, raise_server_exceptions=False)


def _case(**over):
    base = {
        "id": "c1", "dataset_version": "v1", "case_type": "normal",
        "category": "vendas", "split": "train",
        "input_text": "Minha internet caiu, e agora?",
        "expected_output": "diagnóstico", "expected_state": "Recommend",
        "expected_pattern": None, "red_flags": ["senha"], "weight": 2.5,
        "journey": None, "channel": "api", "complexity": None,
    }
    base.update(over)
    return base


# ── Camada pura ───────────────────────────────────────────────────────
def test_template_has_only_header():
    t = template_csv()
    assert t.startswith("﻿"), "sem BOM o Excel quebra os acentos"
    lines = [ln for ln in t.lstrip("﻿").splitlines() if ln.strip()]
    assert len(lines) == 1, "template com linha extra vira caso-lixo no upload"
    assert lines[0].split(",") == GOLD_CSV_COLUMNS


def test_export_parse_roundtrip():
    csv_text = gold_cases_to_csv([_case()])
    rows, errors = parse_gold_csv(csv_text)
    assert errors == []
    assert len(rows) == 1
    r = rows[0]
    assert r["id"] == "c1" and r["split"] == "train"
    d = r["data"]
    assert d["input_text"] == "Minha internet caiu, e agora?"
    assert d["red_flags"] == ["senha"]
    assert d["weight"] == 2.5


def test_parse_semicolon_delimiter_and_comma_decimal():
    """Excel pt-BR: delimitador ';' e decimal '2,5'."""
    txt = ("id;dataset_version;case_type;input_text;expected_output;weight\n"
           ";v2;adversarial;pergunta maliciosa;recusa;2,5\n")
    rows, errors = parse_gold_csv(txt)
    assert errors == []
    assert rows[0]["data"]["weight"] == 2.5
    assert rows[0]["data"]["case_type"] == "adversarial"
    assert rows[0]["data"]["dataset_version"] == "v2"


def test_parse_red_flags_json_and_semicolon_fallback():
    csv_text = gold_cases_to_csv([
        _case(id="a", red_flags=["senha", "CPF"]),
    ])
    rows, _ = parse_gold_csv(csv_text)
    assert rows[0]["data"]["red_flags"] == ["senha", "CPF"]
    txt = ("input_text,expected_output,red_flags\n"
           "oi,resposta,senha;CPF do cliente\n")
    rows, errors = parse_gold_csv(txt)
    assert errors == []
    assert rows[0]["data"]["red_flags"] == ["senha", "CPF do cliente"]


def test_parse_rejects_malformed_red_flags_json():
    """Começa com '[' mas não parseia → ERRO explícito (silenciar geraria
    red_flag errada que nunca dispara)."""
    txt = ('input_text,expected_output,red_flags\n'
           'oi,resposta,"[senha, sem aspas]"\n')
    rows, errors = parse_gold_csv(txt)
    assert rows == []
    assert len(errors) == 1 and "JSON" in errors[0]["motivo"]


def test_parse_errors_carry_physical_line_number():
    txt = ("input_text,expected_output,weight\n"
           "ok,ok,1.0\n"
           ",faltou input,1.0\n"
           "ok de novo,ok,99\n")
    rows, errors = parse_gold_csv(txt)
    assert len(rows) == 1
    assert [e["line"] for e in errors] == [3, 4]
    assert "input_text vazio" in errors[0]["motivo"]
    assert "fora de [0.1, 10.0]" in errors[1]["motivo"]


def test_parse_invalid_split_and_case_type():
    txt = ("input_text,expected_output,case_type,split\n"
           "a,b,esquisito,train\n"
           "c,d,normal,metade\n")
    rows, errors = parse_gold_csv(txt)
    assert rows == []
    assert "case_type inválido" in errors[0]["motivo"]
    assert "split inválido" in errors[1]["motivo"]


def test_parse_unknown_column_fails_fast():
    txt = "input_text,expected_output,coluna_magica\na,b,c\n"
    rows, errors = parse_gold_csv(txt)
    assert rows == []
    assert "colunas desconhecidas" in errors[0]["motivo"]
    assert errors[0]["line"] == 1


def test_parse_skips_fully_empty_lines():
    txt = "input_text,expected_output\noi,tchau\n,,\n\n"
    rows, errors = parse_gold_csv(txt)
    assert len(rows) == 1 and errors == []


def test_parse_tolerates_bom():
    txt = "﻿input_text,expected_output\noi,tchau\n"
    rows, errors = parse_gold_csv(txt)
    assert len(rows) == 1 and errors == []


# ── Rota de export ────────────────────────────────────────────────────
def test_export_route_filters_and_returns_csv(monkeypatch):
    cases = [
        _case(id="a", category="vendas", split="train",
              red_flags=json.dumps(["senha"])),
        _case(id="b", category="suporte", split=None,
              input_text="fatura atrasada", red_flags="[]"),
    ]
    monkeypatch.setattr(dash.gold_cases_repo, "find_all", _async(cases))
    r = _client().get("/api/v1/gold-cases/export?category=suporte&split=sem")
    assert r.status_code == 200, r.text
    assert "text/csv" in r.headers["content-type"]
    assert 'attachment' in r.headers["content-disposition"]
    body = r.text
    assert "fatura atrasada" in body
    assert "Minha internet caiu" not in body, "filtro category/split vazou"


def test_export_route_q_filter(monkeypatch):
    cases = [_case(id="a", red_flags="[]"),
             _case(id="b", input_text="roteador piscando", red_flags="[]")]
    monkeypatch.setattr(dash.gold_cases_repo, "find_all", _async(cases))
    r = _client().get("/api/v1/gold-cases/export?q=roteador")
    assert "roteador piscando" in r.text
    assert "internet caiu" not in r.text


def test_template_route(monkeypatch):
    r = _client().get("/api/v1/gold-cases/import-template")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert "input_text" in r.text


# ── Rota de import ────────────────────────────────────────────────────
def _upload(client, csv_text: str, mode: str, apply_partial=False):
    return client.post(
        "/api/v1/gold-cases/import",
        files={"file": ("gold.csv", csv_text.encode("utf-8"), "text/csv")},
        data={"mode": mode, "apply_partial": str(apply_partial).lower()},
    )


def _allow_admin(monkeypatch):
    monkeypatch.setattr(dash, "require_role",
                        lambda *r: _async({"id": "u1", "role": "admin"}))


def test_import_403_sem_papel(monkeypatch):
    def _deny(*roles):
        async def _fn(request):
            raise HTTPException(403, "Permissão insuficiente")
        return _fn
    monkeypatch.setattr(dash, "require_role", _deny)
    r = _upload(_client(), "input_text,expected_output\na,b\n", "novos")
    assert r.status_code == 403


def test_import_novos_cria_e_rejeita_id_preenchido(monkeypatch):
    _allow_admin(monkeypatch)
    created = []
    async def _create(row):
        created.append(row)
    monkeypatch.setattr(dash.gold_cases_repo, "create", _create)
    txt = ("id,input_text,expected_output,split\n"
           ",caso novo,saida,train\n"
           "abc-123,colou o export,saida,\n")
    r = _upload(_client(), txt, "novos", apply_partial=True)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["criados"] == 1 and body["rejeitados"] == 1
    assert "coluna id preenchida" in body["erros"][0]["motivo"]
    assert len(created) == 1
    assert created[0]["split"] == "train"
    assert created[0]["input_text"] == "caso novo"


def test_import_strict_aplica_nada_com_erro(monkeypatch):
    """Duas fases: qualquer erro + apply_partial=false → 422 e NENHUM
    create/update executado (sem meia-importação silenciosa)."""
    _allow_admin(monkeypatch)
    def _boom(*a, **k):
        raise AssertionError("não deveria aplicar nada no modo estrito")
    monkeypatch.setattr(dash.gold_cases_repo, "create", _boom)
    monkeypatch.setattr(dash.gold_cases_repo, "update", _boom)
    txt = ("input_text,expected_output\nvalida,ok\n,faltou input\n")
    r = _upload(_client(), txt, "novos")
    assert r.status_code == 422, r.text
    det = r.json()["detail"]
    assert det["linhas_validas"] == 1
    assert "NADA foi aplicado" in det["message"]


def test_import_atualizar_por_id(monkeypatch):
    _allow_admin(monkeypatch)
    updates = {}
    async def _find(cid):
        return {"id": cid} if cid == "existe-1" else None
    async def _update(cid, payload):
        updates[cid] = payload
        return True
    monkeypatch.setattr(dash.gold_cases_repo, "find_by_id", _find)
    monkeypatch.setattr(dash.gold_cases_repo, "update", _update)
    txt = ("id,input_text,expected_output,red_flags\n"
           "existe-1,editado,saida,\"[\"\"senha\"\"]\"\n"
           "fantasma,orfao,saida,\n"
           ",sem id,saida,\n")
    r = _upload(_client(), txt, "atualizar", apply_partial=True)
    body = r.json()
    assert body["atualizados"] == 1 and body["rejeitados"] == 2
    assert updates["existe-1"]["input_text"] == "editado"
    assert json.loads(updates["existe-1"]["red_flags"]) == ["senha"]
    motivos = " | ".join(e["motivo"] for e in body["erros"])
    assert "não existe" in motivos and "exige a coluna id" in motivos


def test_import_concatenar_ignora_id(monkeypatch):
    _allow_admin(monkeypatch)
    created = []
    async def _create(row):
        created.append(row)
    monkeypatch.setattr(dash.gold_cases_repo, "create", _create)
    txt = ("id,input_text,expected_output\n"
           "abc-1,duplicando dataset,saida\n")
    r = _upload(_client(), txt, "concatenar")
    assert r.status_code == 200, r.text
    assert r.json()["criados"] == 1
    assert created[0]["id"] != "abc-1", "concatenar deve gerar id NOVO"


def test_import_mode_invalido_e_nao_utf8(monkeypatch):
    _allow_admin(monkeypatch)
    r = _upload(_client(), "input_text,expected_output\na,b\n", "mesclar")
    assert r.status_code == 422
    c = _client()
    r = c.post("/api/v1/gold-cases/import",
               files={"file": ("g.csv", "input_text,expected_output\ná,b\n"
                               .encode("latin-1"), "text/csv")},
               data={"mode": "novos"})
    assert r.status_code == 422
    assert "UTF-8" in r.json()["detail"]
