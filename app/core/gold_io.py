"""Export/import do Golden Dataset em CSV (item 5, 52.0.0).

Funções PURAS (sem I/O de banco) — a rota orquestra; aqui só (de)serialização
e validação linha a linha, testável sem Postgres.

Contrato do arquivo:
- Colunas fixas (GOLD_CSV_COLUMNS); `id` identifica o caso no modo
  "atualizar" e DEVE vir vazio no modo "novos".
- `red_flags`: lista JSON na célula (ex.: ["senha","CPF"]) OU itens
  separados por ponto-e-vírgula (senha;CPF). Em arquivo ';'-delimitado a
  célula com ';' interno PRECISA de aspas ("senha;CPF") — o export/Excel já
  fazem isso; só quem escreve CSV à mão precisa lembrar.
- `split`: '', 'train' ou 'holdout'. No modo 'atualizar', célula vazia
  MANTÉM o valor atual (não apaga).
- Delimitador: vírgula ou ponto-e-vírgula (sniff por linha de cabeçalho —
  Excel pt-BR exporta com ';').
- Encoding: UTF-8 (BOM tolerado na leitura; export inclui BOM para o Excel
  abrir acentos corretamente). Line endings \\r\\n, \\n ou \\r são aceitos.
"""
from __future__ import annotations

import csv
import io
import json

# Ordem estável — o template e o export usam a MESMA lista (round-trip).
GOLD_CSV_COLUMNS = [
    "id", "dataset_version", "case_type", "category", "split",
    "input_text", "expected_output", "expected_state", "expected_pattern",
    "red_flags", "weight", "journey", "channel", "complexity",
]

_VALID_SPLITS = {"", "train", "holdout"}
_VALID_CASE_TYPES = {"normal", "adversarial"}
# O evaluator compara expected_state por IGUALDADE ESTRITA — importar
# 'recommend' minúsculo poluiria a métrica para sempre (state_match nunca
# bate). Normalizamos caixa e validamos contra o conjunto canônico.
_VALID_STATES = {"Recommend", "Refuse", "Escalate"}

# BOM: Excel (Windows) só reconhece UTF-8 com BOM — sem ele, acentos viram
# mojibake ao abrir com duplo clique.
_BOM = "\ufeff"

# Células gigantes (input colado de RAG) estouram o field_size_limit default
# do stdlib (128KB) no MEIO da iteração com csv.Error espúrio — o cap real
# de tamanho é o da rota (5MB).
csv.field_size_limit(10 * 1024 * 1024)


def template_csv() -> str:
    """Template = SÓ o cabeçalho (linha de exemplo criaria caso-lixo se o
    operador esquecesse de apagá-la)."""
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\r\n").writerow(GOLD_CSV_COLUMNS)
    return _BOM + buf.getvalue()


def gold_cases_to_csv(cases: list[dict]) -> str:
    """Linhas do banco → CSV com as colunas do contrato. red_flags (TEXT
    JSON no banco) vai como JSON na célula — round-trip sem perda."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=GOLD_CSV_COLUMNS,
                       extrasaction="ignore", lineterminator="\r\n")
    w.writeheader()
    for c in cases:
        row = {k: c.get(k) for k in GOLD_CSV_COLUMNS}
        rf = row.get("red_flags")
        if isinstance(rf, (list, tuple)):
            row["red_flags"] = json.dumps(list(rf), ensure_ascii=False)
        row = {k: ("" if v is None else v) for k, v in row.items()}
        w.writerow(row)
    return _BOM + buf.getvalue()


def _sniff_delimiter(header_line: str) -> str:
    """',' ou ';' — decide pela contagem na LINHA DE CABEÇALHO (o Sniffer
    do stdlib se perde com células contendo vírgulas de texto livre)."""
    return ";" if header_line.count(";") > header_line.count(",") else ","


def _parse_red_flags(cell: str) -> list[str] | str:
    """Célula → lista. JSON list primeiro; fallback ponto-e-vírgula.
    Retorna str de ERRO quando o JSON é malformado de forma inequívoca
    (começa com '[' mas não parseia) — silenciar geraria red_flag errada."""
    cell = (cell or "").strip()
    if not cell:
        return []
    if cell.startswith("["):
        try:
            val = json.loads(cell)
        except json.JSONDecodeError:
            return "red_flags começa com '[' mas não é JSON válido"
        if not isinstance(val, list):
            return "red_flags JSON precisa ser uma lista de strings"
        return [str(x) for x in val]
    return [p.strip() for p in cell.split(";") if p.strip()]


def _parse_weight(cell: str) -> float | str:
    """Decimal tolerante a pt-BR: '2,5' → 2.5; '1.000,5' → 1000.5 (ponto de
    milhar + vírgula decimal). Retorna str de erro quando não-numérico."""
    c = cell.strip()
    if "." in c and "," in c:
        c = c.replace(".", "").replace(",", ".")
    else:
        c = c.replace(",", ".")
    try:
        return float(c)
    except ValueError:
        return f"weight não numérico '{cell}'"


def parse_gold_csv(text: str, mode: str = "novos") -> tuple[list[dict], list[dict]]:
    """CSV → (linhas válidas, erros). Cada linha válida vira um dict pronto
    para o shape do GoldCaseCreate + {'id','split','provided'} à parte.

    `mode='atualizar'`: input_text/expected_output VAZIOS deixam de ser erro
    — a semântica parcial (célula vazia MANTÉM o valor atual) vale para as
    colunas obrigatórias também; sem isso era impossível editar só um campo
    sem reenviar o resto (achado do E2E ao vivo da própria UI, 52.0.0).

    - `line`: linha FÍSICA do arquivo via reader.line_num (cabeçalho=1) —
      células quoted com quebra de linha e linhas em branco NÃO deslocam a
      contagem (o operador acha a linha certa no editor; em registro
      multiline aponta a ÚLTIMA linha do registro).
    - `provided`: colunas cuja célula veio NÃO-vazia — o modo 'atualizar'
      usa isso para NÃO apagar campos existentes com defaults (mesma classe
      do footgun histórico do PUT /settings que zerava segredos).
    - case_type/split normalizados p/ minúsculas e expected_state p/
      Title-case (Excel autocapitaliza células de texto).
    """
    # \r-only / line endings mistos derrubavam o csv com "new-line character
    # seen in unquoted field" — normaliza ANTES de qualquer parsing.
    text = text.lstrip(_BOM).replace("\r\n", "\n").replace("\r", "\n")
    lines = text.splitlines()
    if not lines or not lines[0].strip():
        return [], [{"line": 1, "motivo": "arquivo vazio ou sem cabeçalho"}]
    delim = _sniff_delimiter(lines[0])
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    header = [h.strip() for h in (reader.fieldnames or [])]
    unknown = [h for h in header if h and h not in GOLD_CSV_COLUMNS]
    missing = [c for c in ("input_text", "expected_output") if c not in header]
    if unknown or missing:
        motivo = []
        if unknown:
            motivo.append(f"colunas desconhecidas: {unknown}")
        if missing:
            motivo.append(f"colunas obrigatórias ausentes: {missing}")
        return [], [{"line": 1, "motivo": "; ".join(motivo) +
                     f" — use o template (colunas: {GOLD_CSV_COLUMNS})"}]

    rows: list[dict] = []
    errors: list[dict] = []
    while True:
        try:
            raw = next(reader)
        except StopIteration:
            break
        except csv.Error as e:
            # aspas desbalanceadas etc. — erro ACIONÁVEL com a linha,
            # nunca 500 na rota. Não dá para retomar o reader com
            # segurança depois de um csv.Error: paramos aqui e reportamos.
            errors.append({"line": reader.line_num,
                           "motivo": f"CSV malformado: {e} — corrija e "
                                     "reenvie (linhas seguintes não foram "
                                     "lidas)"})
            break
        # line_num conta linhas FÍSICAS consumidas (cabeçalho incluso).
        i = reader.line_num
        # Células além do cabeçalho caem no restkey None. Só é ERRO se
        # alguma tiver conteúdo — ',,' sobrando é artefato de Excel e a
        # linha segue o fluxo normal (vazia → skip).
        extra = raw.get(None)
        if extra and any(str(x).strip() for x in extra):
            errors.append({"line": i, "motivo":
                           "linha tem mais células que o cabeçalho "
                           "(vírgula/; sem aspas em algum campo?)"})
            continue
        get = lambda k: (raw.get(k) or "").strip()  # noqa: E731
        provided = {k for k in GOLD_CSV_COLUMNS if get(k)}
        if not provided:
            continue  # linha totalmente vazia — Excel adora criar essas
        problems: list[str] = []
        input_text = get("input_text")
        expected_output = get("expected_output")
        if mode != "atualizar":
            if not input_text:
                problems.append("input_text vazio")
            if not expected_output:
                problems.append("expected_output vazio")
        case_type = get("case_type").lower() or "normal"
        if case_type not in _VALID_CASE_TYPES:
            problems.append(f"case_type inválido '{get('case_type')}' "
                            f"(aceitos: {sorted(_VALID_CASE_TYPES)})")
        split = get("split").lower()
        if split not in _VALID_SPLITS:
            problems.append(f"split inválido '{get('split')}' "
                            "(aceitos: vazio, train, holdout)")
        state_cell = get("expected_state")
        expected_state = state_cell.title() if state_cell else "Recommend"
        if expected_state not in _VALID_STATES:
            problems.append(f"expected_state inválido '{state_cell}' "
                            f"(aceitos: {sorted(_VALID_STATES)})")
        weight = 1.0
        if get("weight"):
            w = _parse_weight(get("weight"))
            if isinstance(w, str):
                problems.append(w)
            elif not (0.1 <= w <= 10.0):
                problems.append(f"weight fora de [0.1, 10.0]: {w}")
            else:
                weight = w
        red_flags = _parse_red_flags(get("red_flags"))
        if isinstance(red_flags, str):
            problems.append(red_flags)
            red_flags = []
        if problems:
            errors.append({"line": i, "motivo": "; ".join(problems)})
            continue
        rows.append({
            "line": i,
            "id": get("id"),
            "split": (split or None),
            "provided": provided,
            "data": {
                "dataset_version": get("dataset_version") or "v1",
                "case_type": case_type,
                "journey": get("journey") or None,
                "channel": get("channel") or "api",
                "complexity": get("complexity") or None,
                "input_text": input_text,
                "expected_output": expected_output,
                "expected_state": expected_state,
                "category": get("category") or None,
                "weight": weight,
                "expected_pattern": get("expected_pattern") or None,
                "red_flags": red_flags,
            },
        })
    return rows, errors
