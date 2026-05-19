"""Pré-checks automáticos executados no momento da submissão.

Onda 1: checks "leves" (sem custo de execução). São relatório para Root,
não bloqueiam submissão. Onda 2+: harness adversarial + capability
fingerprint por execução podem se tornar gates.

Cada check retorna {name, passed, severity, message}. Severity 'error'
agrega em errors_count; 'warning' em warnings_count. Overall `passed`
significa ausência de errors (warnings são informativos).
"""

from __future__ import annotations

import re
from typing import Optional

_SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")

# Tamanhos mínimos esperados — calibração conservadora.
_MIN_DESCRIPTION_CHARS = 20
_MIN_NAME_CHARS = 3


def _check(name: str, passed: bool, message: str, severity: str = "error") -> dict:
    return {"name": name, "passed": passed, "severity": severity, "message": message}


def run_prechecks(
    entry: dict,
    *,
    disclosure: Optional[dict] = None,
    owner: Optional[dict] = None,
    external_metadata: Optional[dict] = None,
    recipe: Optional[dict] = None,
) -> dict:
    """Executa checks sobre uma entry candidata à submissão.

    Args:
        entry: row da catalog_entries (dict).
        disclosure: row de catalog_capability_disclosure (None se ausente).
        owner: row de users (None se não encontrado).
        external_metadata: row de catalog_external_metadata. Só relevante
            quando kind='external_platform'.
        recipe: row de catalog_recipes. Só relevante quando kind='recipe'.

    Returns:
        {checks: [...], passed: bool, errors_count: int, warnings_count: int}
    """
    checks: list[dict] = []

    # 1. Nome substancial
    name = (entry.get("name") or "").strip()
    checks.append(_check(
        "name_length",
        len(name) >= _MIN_NAME_CHARS,
        f"name precisa de ao menos {_MIN_NAME_CHARS} chars" if len(name) < _MIN_NAME_CHARS else "ok",
    ))

    # 2. Descrição substancial — warning para não bloquear iteração
    desc = (entry.get("description") or "").strip()
    checks.append(_check(
        "description_length",
        len(desc) >= _MIN_DESCRIPTION_CHARS,
        f"description curta (<{_MIN_DESCRIPTION_CHARS} chars) — recomendado para Root entender escopo",
        severity="warning",
    ))

    # 3. Version semver
    version = entry.get("version") or ""
    checks.append(_check(
        "version_semver",
        bool(_SEMVER_RE.match(version)),
        f"version '{version}' não é semver MAJOR.MINOR.PATCH",
    ))

    # 4. Owner ativo
    if owner is None:
        checks.append(_check(
            "owner_exists",
            False,
            f"owner_user_id={entry.get('owner_user_id')} não encontrado",
        ))
    else:
        status = owner.get("status", "active")
        checks.append(_check(
            "owner_active",
            status == "active",
            f"owner está com status '{status}' — Root deve confirmar antes de aprovar",
            severity="warning" if status != "active" else "error",  # warning porque entry pode preexistir
        ) if status != "active" else _check("owner_active", True, "ok"))

    # 5. Capability disclosure presente — error a partir do PR 4 (CRUD entregue).
    # Não bloqueia submit (precheck só sinaliza), mas Root deve rejeitar entries
    # sem disclosure declarada — governança interna de IA depende disso (R6.3).
    checks.append(_check(
        "capability_disclosure_present",
        disclosure is not None,
        "capability disclosure ausente — declare em PUT /catalog/entries/{id}/capability",
        severity="error",
    ))

    # 6. Visibility coerente: department exige scope
    visibility = entry.get("visibility")
    scope = entry.get("visibility_scope")
    if visibility == "department":
        checks.append(_check(
            "visibility_scope_for_department",
            bool(scope),
            "visibility='department' exige visibility_scope (nome da área)",
        ))
    else:
        checks.append(_check("visibility_scope_for_department", True, "n/a"))

    # 7. Adapter binding mínimo: a2a exige artifact_id (já validado no create,
    # mas re-verificamos aqui — entry pode ter sido criada antes da regra).
    # Recipe é a2a por convenção mas não tem artifact — pula este check.
    adapter_type = entry.get("adapter_type")
    if adapter_type == "a2a" and entry.get("kind") != "recipe":
        has_artifact = bool(entry.get("artifact_type") and entry.get("artifact_id"))
        checks.append(_check(
            "a2a_has_artifact",
            has_artifact,
            "adapter_type='a2a' exige artifact_type + artifact_id (vínculo a agent/skill interno)",
        ))
    else:
        checks.append(_check("a2a_has_artifact", True, "n/a"))

    # 8. External Platforms (Onda 2): metadata vendor é obrigatório.
    # Warning porque é onda 2 — pode virar error em onda futura.
    if entry.get("kind") == "external_platform":
        has_vendor = bool(external_metadata and external_metadata.get("vendor"))
        checks.append(_check(
            "external_metadata_present",
            has_vendor,
            "kind='external_platform' exige metadata declarada (vendor mínimo) — "
            "PUT /catalog/entries/{id}/external-metadata",
            severity="warning",
        ))
    else:
        checks.append(_check("external_metadata_present", True, "n/a"))

    # 9. Recipes (Onda 3): manifest com pelo menos 1 step.
    # Error porque recipe sem steps é ininteligível.
    if entry.get("kind") == "recipe":
        steps = (recipe or {}).get("steps") or []
        checks.append(_check(
            "recipe_has_steps",
            len(steps) >= 1,
            "kind='recipe' exige pelo menos 1 step — "
            "PUT /catalog/entries/{id}/recipe",
        ))
    else:
        checks.append(_check("recipe_has_steps", True, "n/a"))

    errors = sum(1 for c in checks if not c["passed"] and c["severity"] == "error")
    warnings = sum(1 for c in checks if not c["passed"] and c["severity"] == "warning")

    return {
        "checks": checks,
        "passed": errors == 0,
        "errors_count": errors,
        "warnings_count": warnings,
    }
