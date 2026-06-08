"""Linter de SKILL.md para API Bindings declarativos.

Checks específicos do modo declarativo — complementa a validação
estrutural de `parse_skill_md` com regras semânticas do engine.
"""

import re
from dataclasses import dataclass
from typing import Any


_IDEMPOTENT_METHODS = {"POST", "PATCH", "DELETE"}
_SECRET_REF_RE = re.compile(r"\{\{\s*secrets(\b|\.)", re.IGNORECASE)
_URL_IN_PATH_RE = re.compile(r"^\s*https?://", re.IGNORECASE)


@dataclass
class LintIssue:
    severity: str         # error | warning | info
    binding_id: str       # "*" quando global
    code: str             # ex: "missing_idempotency_key"
    message: str

    def as_dict(self) -> dict:
        return {
            "severity": self.severity,
            "binding_id": self.binding_id,
            "code": self.code,
            "message": self.message,
        }


def _has_mcp_binding(parsed: Any) -> bool:
    """True se a SKILL declara ≥1 tool MCP no ## Tool Bindings.

    Usa `parse_tool_bindings` (fonte canônica) via import preguiçoso — não acopla
    o linter ao módulo MCP no import e evita ciclo. Fail-safe: erro → False.
    """
    tb = (getattr(parsed, "tool_bindings", "") or "").strip()
    if not tb:
        return False
    try:
        from app.mcp.runtime import parse_tool_bindings
        return len(parse_tool_bindings(tb)) > 0
    except Exception:
        return False


def _mcp_inputs_missing_operation(parsed: Any) -> bool:
    """True se a SKILL vincula tool MCP E o ## Inputs tem schema com `properties`
    MAS sem `operation`. Sem ## Inputs (ou sem properties), o runtime cai no
    fallback legacy `{operation, query}` e funciona — então NÃO é violação.
    """
    if not _has_mcp_binding(parsed):
        return False
    try:
        from app.skill_parser.inputs_schema import extract_inputs_schema
        schema = extract_inputs_schema("## Inputs\n" + (getattr(parsed, "inputs", "") or ""))
    except Exception:
        return False
    if not isinstance(schema, dict):
        return False
    props = schema.get("properties") or {}
    if not props:
        return False
    return "operation" not in props


def lint_skill(parsed: Any) -> list[dict]:
    """Retorna lista de issues (dicts) encontradas no SKILL.md.

    Aceita um objeto ParsedSkill (de parser.parse_skill_md).
    """
    issues: list[LintIssue] = []

    exec_mode = getattr(parsed, "execution_mode", "") or ""
    bindings = list(getattr(parsed, "api_bindings_parsed", []) or [])

    # ── Checks globais ──────────────────────────────────────
    # Mensagem alinhada com a do parser (parser.py:202-204) para que o
    # operador não veja dois erros diferentes para a mesma causa em
    # caminhos de validação distintos.
    if exec_mode == "declarative" and not bindings:
        issues.append(LintIssue(
            "error", "*", "declarative_without_bindings",
            "execution_mode=declarative exige ## API Bindings OU ## Data Tables "
            "com pelo menos 1 entrada válida",
        ))

    # ── MCP: ## Inputs deve declarar `operation` (contrato {operation, query}) ──
    # Sem `operation`, o runtime usa o NOME DO SERVIDOR como tool → "Unknown tool"
    # → resposta vazia (bug "tavily a", 2026-06-08). O wizard já força na GERAÇÃO
    # (#324); esta regra cobre edição manual / import / criação 'manual'. Só
    # dispara quando há schema de domínio SEM operation — sem ## Inputs, o runtime
    # cai no fallback legacy {operation, query} e funciona.
    if _mcp_inputs_missing_operation(parsed):
        issues.append(LintIssue(
            "error", "*", "mcp_inputs_missing_operation",
            "Skill vincula tool MCP mas o ## Inputs não declara `operation`. O "
            "runtime espera `{operation, query}` — sem `operation`, a chamada vira "
            "o nome do servidor e o servidor MCP responde 'Unknown tool' (resposta "
            "vazia). Declare `operation` (e `query`) no ## Inputs.",
        ))

    # Duplicatas de binding_id
    seen: dict[str, int] = {}
    for b in bindings:
        bid = b.get("id", "")
        if not bid:
            issues.append(LintIssue(
                "error", "?", "missing_binding_id",
                "binding sem campo 'id'",
            ))
            continue
        seen[bid] = seen.get(bid, 0) + 1
    for bid, count in seen.items():
        if count > 1:
            issues.append(LintIssue(
                "error", bid, "duplicate_binding_id",
                f"binding_id '{bid}' aparece {count} vezes",
            ))

    # ── Checks por binding ──────────────────────────────────
    all_ids = set(seen.keys())
    for b in bindings:
        bid = b.get("id") or "?"

        # connector obrigatório (aceita `connector` ou `connector_id` como
        # alias — wizard emite ambos, SKILL.md à mão tende a usar `connector`).
        if not (b.get("connector") or b.get("connector_id")):
            issues.append(LintIssue(
                "error", bid, "missing_connector",
                "campo 'connector' (ou 'connector_id') obrigatório",
            ))

        # method + path
        method = (b.get("method") or "GET").upper()
        path = b.get("path") or ""
        if not path:
            issues.append(LintIssue(
                "error", bid, "missing_path",
                "campo 'path' obrigatório",
            ))
        elif _URL_IN_PATH_RE.match(path):
            issues.append(LintIssue(
                "error", bid, "absolute_url_in_path",
                "'path' não pode conter esquema http(s):// — o host vem do connector",
            ))

        # idempotency para métodos mutáveis
        idem = b.get("idempotency_key") or ""
        if method in _IDEMPOTENT_METHODS and not idem:
            issues.append(LintIssue(
                "error", bid, "missing_idempotency_key",
                f"idempotency_key obrigatório para {method}",
            ))

        # output_mapping
        om = b.get("output_mapping") or []
        if not om:
            if exec_mode == "declarative":
                # O runtime declarativo EXIGE output_mapping (declarative_engine:
                # "output_mapping é obrigatório"). Sem ele a chamada dá 2xx, mas o
                # engine marca erro → final_state=partial → a UI mostra "resultado
                # parcial" e nada chega ao context. Bloqueia no /lint (UI marca
                # vermelho) ANTES do save, em vez de falhar opaco em runtime —
                # causa-raiz do bug "Consulta de CEP" (2026-06-07). Skills NÃO
                # declarativas seguem com warning (bindings só rodam em declarative).
                issues.append(LintIssue(
                    "error", bid, "missing_output_mapping_declarative",
                    "output_mapping obrigatório em binding declarativo — sem ele o "
                    "engine devolve 'resultado parcial'. Mapeie a resposta da API "
                    "(ex.: '- from: $.campo' + 'to: chave_no_context').",
                ))
            else:
                issues.append(LintIssue(
                    "warning", bid, "empty_output_mapping",
                    "output_mapping vazio — binding não contribui ao context",
                ))
        else:
            for m in om:
                if not isinstance(m, dict):
                    issues.append(LintIssue(
                        "error", bid, "invalid_mapping_item",
                        f"item de output_mapping não é objeto: {m!r}",
                    ))
                    continue
                if not m.get("from"):
                    issues.append(LintIssue(
                        "error", bid, "mapping_missing_from",
                        "entrada de output_mapping sem 'from' (JSONPath)",
                    ))
                if not m.get("to"):
                    issues.append(LintIssue(
                        "error", bid, "mapping_missing_to",
                        "entrada de output_mapping sem 'to' (caminho no context)",
                    ))

        # depends_on existe?
        deps = b.get("depends_on") or []
        if isinstance(deps, str):
            deps = [deps]
        for d in deps:
            if d not in all_ids:
                issues.append(LintIssue(
                    "error", bid, "unknown_dependency",
                    f"depends_on '{d}' não existe entre os bindings",
                ))
            if d == bid:
                issues.append(LintIssue(
                    "error", bid, "self_dependency",
                    "binding não pode depender de si mesmo",
                ))

        # on_failure: compensate aponta para binding existente?
        of = b.get("on_failure")
        comp_target = None
        if isinstance(of, dict) and "compensate" in of:
            comp_target = of["compensate"]
        elif isinstance(of, str) and of.startswith("compensate:"):
            comp_target = of.split(":", 1)[1].strip()
        if comp_target and comp_target not in all_ids:
            issues.append(LintIssue(
                "error", bid, "unknown_compensate_target",
                f"on_failure.compensate='{comp_target}' não existe entre os bindings",
            ))
        if comp_target and comp_target == bid:
            issues.append(LintIssue(
                "error", bid, "self_compensation",
                "binding não pode compensar a si mesmo",
            ))

        # Secrets em templates (fora da área de auth, que nem chega ao scope)
        risky_fields = [
            ("path", b.get("path")),
            ("query", _flatten_values(b.get("query"))),
            ("body", _flatten_values(b.get("body"))),
            ("headers", _flatten_values(b.get("headers"))),
            ("idempotency_key", b.get("idempotency_key")),
        ]
        for field_name, value in risky_fields:
            for s in _iter_strings(value):
                if s and _SECRET_REF_RE.search(s):
                    issues.append(LintIssue(
                        "error", bid, "secret_leak_in_template",
                        f"template em '{field_name}' referencia secrets.* — proibido; "
                        "secrets só podem entrar via auth do connector",
                    ))
                    break

    # Ciclos no DAG — best-effort
    cycle_ids = _detect_cycle(bindings)
    if cycle_ids:
        issues.append(LintIssue(
            "error", "*", "dag_cycle",
            f"ciclo detectado em depends_on: {sorted(cycle_ids)}",
        ))

    # Output Contract.title precisa casar com ^[a-zA-Z0-9_-]+$ — vira name
    # do response_format.json_schema na chamada à OpenAI (engine.py:_build
    # _response_format). Pegar isso em runtime já está sanitizado (helper
    # sanitize_schema_name), mas o lint avisa em tempo de edição para o
    # operador entender o que sai como identifier. Reportado por user
    # 2026-06-01 com title="Saida da Categorizar Imagem".
    output_contract = getattr(parsed, "output_contract", "") or ""
    raw_title = _extract_json_schema_title(output_contract)
    if raw_title:
        from app.core.text_utils import schema_name_is_valid, sanitize_schema_name
        if not schema_name_is_valid(raw_title):
            sanitized = sanitize_schema_name(raw_title)
            issues.append(LintIssue(
                "warning", "*", "output_contract_title_invalid_chars",
                f"## Output Contract.title='{raw_title}' tem chars fora de "
                f"^[a-zA-Z0-9_-]+$ — runtime sanitiza para '{sanitized}', mas "
                "considere editar o title no SKILL.md para combinar.",
            ))

    return [i.as_dict() for i in issues]


def _flatten_values(obj: Any) -> list:
    """Coleta recursivamente todos os valores escalares de um dict/list."""
    out: list = []
    if isinstance(obj, dict):
        for v in obj.values():
            out.extend(_flatten_values(v))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_flatten_values(v))
    elif obj is not None:
        out.append(obj)
    return out


def _iter_strings(obj: Any):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_strings(v)


def _extract_json_schema_title(output_contract_text: str) -> str:
    """Extrai `title` do bloco JSON Schema dentro de ## Output Contract.

    Retorna `""` quando não há fence ```json, quando o JSON é inválido, ou
    quando o schema não declara `title`. Best-effort — falha silenciosa para
    não derrubar todo o linter por um Output Contract mal formatado (outros
    checks já cobrem isso).
    """
    if not output_contract_text:
        return ""
    import json as _json
    match = re.search(r"```(?:json)?\s*\n(.*?)\n```", output_contract_text, re.DOTALL)
    if not match:
        return ""
    try:
        data = _json.loads(match.group(1))
    except (ValueError, TypeError):
        return ""
    title = data.get("title") if isinstance(data, dict) else None
    return str(title) if isinstance(title, str) else ""


def _detect_cycle(bindings: list[dict]) -> set[str]:
    """Retorna set de ids envolvidos em ciclo (ou vazio)."""
    by_id = {b.get("id"): b for b in bindings if b.get("id")}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {bid: WHITE for bid in by_id}
    in_cycle: set[str] = set()

    def visit(bid: str, stack: list[str]) -> None:
        color[bid] = GRAY
        stack.append(bid)
        deps = by_id[bid].get("depends_on") or []
        if isinstance(deps, str):
            deps = [deps]
        for d in deps:
            if d not in by_id:
                continue
            if color[d] == GRAY:
                # ciclo: tudo em stack de d até o topo
                start_idx = stack.index(d) if d in stack else 0
                for x in stack[start_idx:]:
                    in_cycle.add(x)
            elif color[d] == WHITE:
                visit(d, stack)
        stack.pop()
        color[bid] = BLACK

    for bid in by_id:
        if color[bid] == WHITE:
            visit(bid, [])
    return in_cycle
