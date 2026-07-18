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
    # escape EXPLÍCITO (﻿): com o caractere literal, um editor que
    # limpe BOMs transformaria o assert em startswith('') == sempre True.
    assert t.startswith("\ufeff"), "sem BOM o Excel quebra os acentos"
    lines = [ln for ln in t.lstrip("\ufeff").splitlines() if ln.strip()]
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
    txt = "\ufeffinput_text,expected_output\noi,tchau\n"
    rows, errors = parse_gold_csv(txt)
    assert len(rows) == 1 and errors == []


# ── Endurecimento pós-revisão adversarial (18 achados) ────────────────
def test_parse_physical_line_number_with_multiline_cell_and_blank_lines():
    """[review 1/13] célula quoted multiline consome várias linhas físicas
    e linha em branco é pulada pelo DictReader — o número reportado precisa
    seguir o ARQUIVO (reader.line_num), não o índice do registro."""
    txt = ('input_text,expected_output\n'          # linha 1
           '"multi\nlinha\naqui",ok\n'             # linhas 2-4 (1 registro)
           '\n'                                    # linha 5 (em branco)
           ',faltou input\n')                      # linha 6 ← o erro
    rows, errors = parse_gold_csv(txt)
    assert len(rows) == 1
    assert rows[0]["data"]["input_text"] == "multi\nlinha\naqui"
    assert errors[0]["line"] == 6, (
        f"linha reportada {errors[0]['line']} — o operador corrigiria a "
        "linha ERRADA no editor"
    )


def test_parse_cr_only_line_endings_do_not_crash():
    """[review 2] \\r-only (Mac antigo) derrubava o csv com _csv.Error →
    500 na rota. Normalização de line endings resolve ANTES do reader."""
    txt = "input_text,expected_output\ra,b\r"
    rows, errors = parse_gold_csv(txt)
    assert len(rows) == 1 and errors == []
    assert rows[0]["data"]["input_text"] == "a"


def test_parse_csv_error_reported_not_raised():
    """[review 2] csv.Error no meio da iteração vira erro ACIONÁVEL com
    linha, nunca exceção → 500 na rota. Em produção a classe é neutralizada
    upstream (line endings normalizados + field_size_limit 10MB); o branch
    é o cinto-e-suspensório — testado baixando o limite temporariamente."""
    import csv as _csv
    old = _csv.field_size_limit(64)
    try:
        txt = ("input_text,expected_output\n"
               f"\"{'x' * 500}\",estoura o limite\n")
        rows, errors = parse_gold_csv(txt)
    finally:
        _csv.field_size_limit(old)
    assert rows == []
    assert errors and "CSV malformado" in errors[0]["motivo"]


def test_parse_normalizes_excel_capitalization():
    """[review 6] Excel autocapitaliza células: 'Normal', 'TRAIN' e
    'recommend' precisam ser aceitos com normalização de caixa."""
    txt = ("input_text,expected_output,case_type,split,expected_state\n"
           "a,b,Normal,TRAIN,recommend\n")
    rows, errors = parse_gold_csv(txt)
    assert errors == []
    d = rows[0]
    assert d["data"]["case_type"] == "normal"
    assert d["split"] == "train"
    assert d["data"]["expected_state"] == "Recommend"


def test_parse_rejects_invalid_expected_state():
    """[review 3] o evaluator compara por igualdade estrita — estado
    inválido importado em silêncio poluiria a métrica para sempre."""
    txt = ("input_text,expected_output,expected_state\n"
           "a,b,Aprovar\n")
    rows, errors = parse_gold_csv(txt)
    assert rows == []
    assert "expected_state inválido" in errors[0]["motivo"]


def test_parse_weight_thousand_separator_ptbr():
    """[review 5] '1.000,5' = mil ponto cinco pt-BR → 1000.5 → erro de
    range CLARO (não 'não numérico')."""
    txt = "input_text,expected_output,weight\na,b,\"1.000,5\"\n"
    rows, errors = parse_gold_csv(txt)
    assert rows == []
    assert "fora de [0.1, 10.0]: 1000.5" in errors[0]["motivo"]


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


def test_import_atualizar_preserva_campos_nao_enviados(monkeypatch):
    """[review 10/18] célula vazia MANTÉM o valor atual — update PARCIAL.
    Mesma classe do footgun histórico do PUT /settings que zerava segredos:
    um CSV só com id+input_text não pode apagar split/red_flags/weight."""
    _allow_admin(monkeypatch)
    updates = {}
    async def _find(cid):
        return {"id": cid}
    async def _update(cid, payload):
        updates[cid] = payload
        return True
    monkeypatch.setattr(dash.gold_cases_repo, "find_by_id", _find)
    monkeypatch.setattr(dash.gold_cases_repo, "update", _update)
    txt = ("id,input_text,expected_output,split,weight\n"
           "caso-1,texto novo,saida nova,,\n")
    r = _upload(_client(), txt, "atualizar")
    assert r.status_code == 200, r.text
    p = updates["caso-1"]
    assert p["input_text"] == "texto novo"
    assert "split" not in p, "célula vazia APAGOU o split (footgun settings)"
    assert "weight" not in p, "célula vazia sobrescreveu weight com default"
    assert "red_flags" not in p, "red_flags resetada sem célula preenchida"
    assert "case_type" not in p and "expected_state" not in p


def test_import_atualizar_id_duplicado_no_arquivo(monkeypatch):
    """[review 12] segundo update do MESMO id no arquivo = last-wins
    silencioso — a 2ª ocorrência é rejeitada apontando a linha da 1ª."""
    _allow_admin(monkeypatch)
    monkeypatch.setattr(dash.gold_cases_repo, "find_by_id",
                        _async({"id": "x"}))
    updates = []
    async def _update(cid, payload):
        updates.append(cid)
        return True
    monkeypatch.setattr(dash.gold_cases_repo, "update", _update)
    txt = ("id,input_text,expected_output\n"
           "dup-1,versao um,s\n"
           "dup-1,versao dois,s\n")
    r = _upload(_client(), txt, "atualizar", apply_partial=True)
    body = r.json()
    assert body["atualizados"] == 1 and body["rejeitados"] == 1
    assert "duplicado" in body["erros"][0]["motivo"]
    assert updates == ["dup-1"], "aplicou os dois updates (last-wins)"


def test_import_atualizar_update_false_nao_mente(monkeypatch):
    """[review 7/15] caso deletado ENTRE a fase 1 e a fase 2: update()
    devolve False e a linha vai para rejeitadas — reportar 'atualizado'
    seria mentira no relatório."""
    _allow_admin(monkeypatch)
    monkeypatch.setattr(dash.gold_cases_repo, "find_by_id",
                        _async({"id": "zumbi"}))
    monkeypatch.setattr(dash.gold_cases_repo, "update", _async(False))
    txt = "id,input_text,expected_output\nzumbi,a,b\n"
    r = _upload(_client(), txt, "atualizar")
    body = r.json()
    assert body["atualizados"] == 0
    assert body["rejeitados"] == 1
    assert "removido entre" in body["erros"][0]["motivo"]


def test_import_apply_partial_todas_invalidas_aplica_zero(monkeypatch):
    """[review 17] apply_partial=true com TODAS as linhas inválidas: 200
    honesto com 0 aplicados e as rejeições no relatório (nada explode,
    nada é criado)."""
    _allow_admin(monkeypatch)
    def _boom(*a, **k):
        raise AssertionError("nada deveria ser criado")
    monkeypatch.setattr(dash.gold_cases_repo, "create", _boom)
    txt = "input_text,expected_output\n,so falta\n,tudo errado\n"
    r = _upload(_client(), txt, "novos", apply_partial=True)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["criados"] == 0 and body["rejeitados"] == 2
    assert "0 criado(s)" in body["message"]


def test_export_413_acima_do_teto(monkeypatch):
    """[review 8/16] dataset maior que o teto: 413 pedindo filtro — nunca
    truncar em silêncio (o operador baixaria um 'backup' incompleto)."""
    grandes = [_case(id=str(i), red_flags="[]") for i in range(10001)]
    monkeypatch.setattr(dash.gold_cases_repo, "find_all", _async(grandes))
    r = _client().get("/api/v1/gold-cases/export")
    assert r.status_code == 413
    assert "filtre" in r.json()["detail"]


def test_export_filename_sanitizado(monkeypatch):
    """[review 9] dataset_version com aspas/unicode no header quebrava o
    encode do h11 (500) — filename só com [A-Za-z0-9._-]."""
    monkeypatch.setattr(dash.gold_cases_repo, "find_all",
                        _async([_case(red_flags="[]")]))
    r = _client().get('/api/v1/gold-cases/export?dataset_version=v"1—x')
    assert r.status_code == 200, r.text
    cd = r.headers["content-disposition"]
    fname = cd.split('filename="')[1].rstrip('"')
    assert fname == "gold_cases_v_1_x.csv", cd


# ── UI do CSV no harness (parte 2 do item 5) ──────────────────────────
import re
from pathlib import Path

_HARNESS = Path(__file__).resolve().parents[1] / "app" / "templates" / "pages" / "harness.html"


def _ui_src() -> str:
    return _HARNESS.read_text(encoding="utf-8")


def _ui_block(start_tid: str, end_tid: str) -> str:
    """Janela entre dois data-testids ESTÁVEIS — regex '.*?</div>' não-guloso
    cortava no primeiro </div> aninhado (fragilidade da revisão [11/12])."""
    src = _ui_src()
    i = src.find(f'data-testid="{start_tid}"')
    j = src.find(f'data-testid="{end_tid}"')
    assert i != -1, f"{start_tid} ausente"
    assert j != -1 and j > i, f"{end_tid} ausente ou fora de ordem"
    return src[i:j]


def test_ui_csv_buttons_in_filter_bar():
    """template/exportar/importar vivem NA BARRA DE FILTROS — o export usa
    o escopo do filtro atual (contrato do item 5)."""
    blk = _ui_block("gold-filter-bar", "gold-csv-import-panel")
    for tid in ("gold-csv-template", "gold-csv-export",
                "gold-csv-import-toggle"):
        assert f'data-testid="{tid}"' in blk, f"botão {tid} fora da barra"
    # [review 3] o export server-side cobre o ESCOPO INTEIRO — o tooltip
    # precisa avisar quando o contador só viu a janela carregada
    assert "TODO o escopo do filtro no servidor" in blk, (
        "botão exportar sem o aviso de divergência contador × arquivo"
    )


def test_ui_export_url_carries_filter_scope():
    src = _ui_src()
    m = re.search(r"_goldExportUrl\(\)\{.*?\n    \},", src, re.S)
    assert m, "_goldExportUrl ausente"
    blk = m.group(0)
    # [review 10] pares LITERAIS chave-do-filtro → parâmetro — substring
    # solta deixava passar regressão que perdesse o mapeamento
    for pair in ("p.set('dataset_version',f.version)",
                 "p.set('case_type',f.case_type)",
                 "p.set('category',f.category)",
                 "p.set('split',f.split)",
                 "p.set('q',f.q)"):
        assert pair.replace(" ", "") in blk.replace(" ", ""), (
            f"export perdeu o mapeamento {pair}"
        )


def test_ui_import_panel_has_modes_and_report():
    blk = _ui_block("gold-csv-import-panel", "gold-list-scroll")
    for mode in ("novos", "atualizar", "concatenar"):
        assert f'value="{mode}"' in blk, f"modo {mode} fora do select"
    assert 'data-testid="gold-csv-import-report"' in blk
    assert "erros_truncados" in blk, "relatório não expõe truncamento"
    assert "célula vazia MANTÉM" in blk, (
        "painel não documenta a semântica parcial do modo atualizar"
    )


def test_ui_import_uses_multipart_and_reloads():
    src = _ui_src()
    m = re.search(r"async importGoldCsv\(\)\{.*?\n    \},", src, re.S)
    assert m, "importGoldCsv ausente"
    blk = m.group(0)
    assert "FormData" in blk, "import sem multipart"
    assert "apply_partial" in blk
    assert "await this.load()" in blk, "lista não recarrega após import"
    # 422 estrito: relatório vem em detail SEM `rejeitados` — normalizado
    # somando os truncados ([review 4b]: só .length subcontava >200 erros)
    assert "rejeitados:((det.erros||[]).length+(det.erros_truncados||0))" in blk, (
        "422 sem normalização de rejeitados — painel pintaria verde no erro"
    )


def test_parse_atualizar_mode_allows_empty_core_fields():
    """Achado do E2E ao vivo da UI (52.0.0): no modo atualizar a semântica
    parcial vale TAMBÉM para input/output — célula vazia mantém o valor,
    senão é impossível editar um campo sem reenviar o resto."""
    txt = "id,input_text,expected_output\nabc,Só o input muda,\n"
    rows, errors = parse_gold_csv(txt, mode="atualizar")
    assert errors == []
    assert rows[0]["provided"] == {"id", "input_text"}
    # nos modos de criação continua obrigatório
    rows2, errors2 = parse_gold_csv(txt, mode="novos")
    assert rows2 == [] and "expected_output vazio" in errors2[0]["motivo"]


def test_import_atualizar_edits_single_field_end_to_end(monkeypatch):
    """Rota + parser juntos: CSV só com id+input_text atualiza SÓ o input."""
    _allow_admin(monkeypatch)
    updates = {}
    async def _find(cid):
        return {"id": cid}
    async def _update(cid, payload):
        updates[cid] = payload
        return True
    monkeypatch.setattr(dash.gold_cases_repo, "find_by_id", _find)
    monkeypatch.setattr(dash.gold_cases_repo, "update", _update)
    txt = "id,input_text,expected_output\ncaso-9,Novo texto,\n"
    r = _upload(_client(), txt, "atualizar")
    assert r.status_code == 200, r.text
    assert r.json()["atualizados"] == 1
    assert updates["caso-9"] == {"input_text": "Novo texto"}


def test_ui_download_surfaces_http_errors():
    """[review 2] âncora crua era cega a 413/401 — o download agora passa
    por fetch, checa resp.ok e mostra o detail acionável no toast."""
    src = _ui_src()
    m = re.search(r"async _download\(url\)\{.*?\n    \},", src, re.S)
    assert m, "_download ausente ou não-async"
    blk = m.group(0)
    assert "resp.ok" in blk, "download não checa status HTTP"
    assert "showToast" in blk, "erro de download sem feedback na página"
    assert "createObjectURL" in blk and "revokeObjectURL" in blk


def test_ui_file_kept_when_rows_rejected():
    """[review 14] com rejeitadas o operador está DEPURANDO o arquivo —
    limpar o input só em sucesso total."""
    src = _ui_src()
    assert "if(!(body.rejeitados>0)) inp.value=''" in src, (
        "input de arquivo limpo mesmo com linhas rejeitadas"
    )


def test_ui_422_normalization_guards_array_detail():
    """[review 8] detail em Array (RequestValidationError) num spread de
    objeto viraria message undefined."""
    src = _ui_src()
    assert "!Array.isArray(det)" in src, (
        "normalização do 422 sem guard de Array"
    )
    # [review 4b] rejeitados soma os truncados — subcontagem em >200 erros
    assert "(det.erros_truncados||0)" in src


def test_filter_semantics_parity_js_python():
    """[review 13] a semântica do filtro do Gold vive DUPLICADA: JS
    (filteredGoldCases, contador) e Python (_gold_export_filter, arquivo
    baixado). Este teste trava os marcadores dos DOIS lados — quem mudar
    um sem o outro quebra aqui e descobre a duplicação."""
    js = _ui_src()
    py = (Path(__file__).resolve().parents[1] / "app" / "routes" /
          "dashboard.py").read_text(encoding="utf-8")
    # split 'sem' (casos ainda não divididos) nos dois lados
    assert "f.split==='sem'" in js
    assert 'split == "sem"' in py
    # busca q varre input E output nos dois lados
    assert "input_text" in js and "expected_output" in js
    m = re.search(r"def _gold_export_filter.*?\n\n", py, re.S)
    assert m, "_gold_export_filter ausente"
    assert "input_text" in m.group(0) and "expected_output" in m.group(0), (
        "q do export não varre os mesmos campos do filtro da UI"
    )


def test_import_atualizar_id_only_rejected_honestly(monkeypatch):
    """[review 1] linha só com id passava a fase 1 e caía na fase 2 com o
    motivo FALSO de deleção concorrente + 'reenvie' em loop eterno — agora
    é rejeitada na FASE 1 com a verdade (e preserva o tudo-ou-nada)."""
    _allow_admin(monkeypatch)
    def _boom(*a, **k):
        raise AssertionError("modo estrito não deveria aplicar nada")
    monkeypatch.setattr(dash.gold_cases_repo, "update", _boom)
    monkeypatch.setattr(dash.gold_cases_repo, "find_by_id",
                        _async({"id": "x"}))
    txt = "id,input_text,expected_output\nso-id,,\n"
    r = _upload(_client(), txt, "atualizar")
    assert r.status_code == 422, r.text
    det = r.json()["detail"]
    assert "nenhuma célula preenchida além do id" in det["erros"][0]["motivo"]
    assert "removido entre" not in str(det), "voltou o motivo mentiroso"


def test_import_report_errors_sorted_after_phase2(monkeypatch):
    """[review 4] erro da fase 2 (deleção concorrente) precisa entrar
    ORDENADO por linha no relatório — o cap de 200 corta pelo fim."""
    _allow_admin(monkeypatch)
    async def _find(cid):
        return {"id": cid}
    monkeypatch.setattr(dash.gold_cases_repo, "find_by_id", _find)
    monkeypatch.setattr(dash.gold_cases_repo, "update", _async(False))
    txt = ("id,input_text,expected_output\n"
           "zumbi,linha dois,x\n"
           ",faltou id na linha tres,x\n")
    r = _upload(_client(), txt, "atualizar", apply_partial=True)
    body = r.json()
    lines = [e["line"] for e in body["erros"]]
    assert lines == sorted(lines), (
        f"erros fora de ordem: {lines} — fase 2 anexou depois do sort"
    )
