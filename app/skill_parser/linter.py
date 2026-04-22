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


def lint_skill(parsed: Any) -> list[dict]:
    """Retorna lista de issues (dicts) encontradas no SKILL.md.

    Aceita um objeto ParsedSkill (de parser.parse_skill_md).
    """
    issues: list[LintIssue] = []

    exec_mode = getattr(parsed, "execution_mode", "") or ""
    bindings = list(getattr(parsed, "api_bindings_parsed", []) or [])

    # ── Checks globais ──────────────────────────────────────
    if exec_mode == "declarative" and not bindings:
        issues.append(LintIssue(
            "error", "*", "declarative_without_bindings",
            "execution_mode=declarative exige pelo menos 1 binding em ## API Bindings",
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

        # connector obrigatório
        if not b.get("connector"):
            issues.append(LintIssue(
                "error", bid, "missing_connector",
                "campo 'connector' obrigatório",
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
        is_compensation_target = False  # checado adiante
        if not om:
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
