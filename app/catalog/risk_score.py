"""Risk score, compliance matcher e alertas automáticos do Inventário Regulatório.

Helpers puros (sem I/O) e determinísticos — pesos hardcoded e documentados.
Decisão consciente: pesos não são editáveis via UI por enquanto, são contrato.
Quando regras evoluírem (ex: ANPD publicar guia novo de retenção), bump
explicitamente neste módulo e versionar.

Estrutura do disclosure esperado (subset relevante de catalog_capability_disclosure):
    reads_user_kb, writes_user_kb, stores_input, storage_retention_days,
    calls_external_apis, external_apis_list, accesses_internet,
    processes_pii, processes_financial, processes_health,
    trains_on_input, output_is_deterministic,
    data_residency, additional_notes

Convenções:
- disclosure=None significa "não declarado" → score 0 + alerta "Capability não declarada"
- score sempre clamped em [0, 100]
- compliance retorna dict com booleanos + razão por regulação
- alerts retorna lista ordenada por severidade (danger → warning → info)
"""

from __future__ import annotations

from typing import Any, Optional


# ─── Risk Score ────────────────────────────────────────────────────
# Pesos calibrados conservadoramente. Soma máxima possível atinge ~107 antes
# do cap em 100 — entries acima de 60 são consideradas alto risco automaticamente.
# Histórico de calibração: PII/Financial/Health pesam igual (20) porque LGPD
# trata dados sensíveis (saúde, biometria, etc.) com mesma criticidade que
# dados identificáveis genéricos. Output determinístico reduz risco (-5) porque
# previne "vazamento por geração" — modelo não inventa dado sensível novo.

_RISK_WEIGHTS = {
    "processes_pii":        20,
    "processes_financial":  20,
    "processes_health":     20,
    "calls_external_apis":  10,
    "accesses_internet":    10,
    "trains_on_input":      10,
    "stores_input":          5,
    "writes_user_kb":        5,
    "reads_user_kb":         2,
}

# Penalidade extra: stores_input=True sem retention declarada → +5
# (LGPD art. 16 — dados pessoais só podem ser retidos pelo tempo necessário;
# sem prazo = retenção indefinida = violação potencial)
_PENALTY_STORE_WITHOUT_RETENTION = 5

# Boost negativo: output determinístico reduz risco (mas mínimo 0)
_DETERMINISTIC_BONUS = -5

_RISK_BAND_LOW = 30        # 0-30
_RISK_BAND_MEDIUM_MAX = 60  # 31-60 = médio; 61+ = alto


def compute_risk_score(disclosure: Optional[dict]) -> dict:
    """Calcula o risk score 0-100 e a banda (low/medium/high).

    Retorna {score, band, breakdown, drivers}:
    - score: int 0-100
    - band: 'low' | 'medium' | 'high'
    - breakdown: dict flag → peso aplicado (para tooltip explicativo)
    - drivers: lista das flags que mais pesam (para narrativa "alto risco porque X, Y")

    disclosure=None ou {} → score=0, band='low', breakdown vazio, drivers=[].
    """
    if not disclosure:
        return {"score": 0, "band": "low", "breakdown": {}, "drivers": []}

    breakdown: dict[str, int] = {}
    for flag, weight in _RISK_WEIGHTS.items():
        if disclosure.get(flag) is True:
            breakdown[flag] = weight

    # Penalidade retention
    if disclosure.get("stores_input") is True:
        retention = disclosure.get("storage_retention_days")
        if retention is None or (isinstance(retention, int) and retention > 365):
            breakdown["__no_retention_penalty__"] = _PENALTY_STORE_WITHOUT_RETENTION

    # Bonus determinístico (negativo, mas só se já há algum risco — não dá pontos
    # de graça pra entry sem nenhuma flag)
    if breakdown and disclosure.get("output_is_deterministic") is True:
        breakdown["__deterministic_bonus__"] = _DETERMINISTIC_BONUS

    score = sum(breakdown.values())
    score = max(0, min(100, score))

    if score <= _RISK_BAND_LOW:
        band = "low"
    elif score <= _RISK_BAND_MEDIUM_MAX:
        band = "medium"
    else:
        band = "high"

    # Drivers: top 3 flags positivas (ignora bonus/penalty internos)
    positive_flags = {k: v for k, v in breakdown.items() if not k.startswith("__") and v > 0}
    drivers = sorted(positive_flags.items(), key=lambda kv: kv[1], reverse=True)[:3]
    drivers_list = [k for k, _ in drivers]

    return {
        "score": score,
        "band": band,
        "breakdown": breakdown,
        "drivers": drivers_list,
    }


# ─── Compliance matcher (LGPD / GDPR / HIPAA / Marco Civil) ────────


def compute_compliance(disclosure: Optional[dict]) -> dict:
    """Mapeia capability disclosure para regulações aplicáveis.

    Retorna {regulation_key: {applies: bool, reason: str}, ...} para:
    - lgpd:         Lei Geral de Proteção de Dados (Brasil, lei 13.709/2018)
    - lgpd_sensitive: LGPD art. 11 — dados sensíveis (saúde, biometria, raça...)
    - gdpr:         General Data Protection Regulation (UE, 2016/679)
    - hipaa:        Health Insurance Portability and Accountability Act (EUA)
    - marco_civil:  Lei 12.965/2014 — uso da internet no Brasil

    Não é parecer jurídico — é checklist objetivo baseado nas flags. Comitê
    valida e refina. Quando ambíguo, retorna applies=True com reason="potencial".
    """
    if not disclosure:
        return {
            "lgpd":            {"applies": False, "reason": "Capability não declarada — comitê deve revisar manualmente"},
            "lgpd_sensitive":  {"applies": False, "reason": "Capability não declarada"},
            "gdpr":            {"applies": False, "reason": "Capability não declarada"},
            "hipaa":           {"applies": False, "reason": "Capability não declarada"},
            "marco_civil":     {"applies": False, "reason": "Capability não declarada"},
        }

    pii = disclosure.get("processes_pii") is True
    financial = disclosure.get("processes_financial") is True
    health = disclosure.get("processes_health") is True
    residency = (disclosure.get("data_residency") or "").upper().strip()
    accesses_internet = disclosure.get("accesses_internet") is True

    out: dict = {}

    # LGPD — aplica se processa qualquer dado pessoal identificável OU dados
    # financeiros/saúde de brasileiros (extraterritorialidade implícita: maioria
    # das entries da plataforma serve usuários BR).
    if pii or financial or health:
        triggers = []
        if pii: triggers.append("PII")
        if financial: triggers.append("dados financeiros")
        if health: triggers.append("dados de saúde")
        out["lgpd"] = {
            "applies": True,
            "reason": f"Processa {', '.join(triggers)} — LGPD se aplica integralmente",
        }
    else:
        out["lgpd"] = {"applies": False, "reason": "Não processa dados pessoais identificáveis"}

    # LGPD art. 11 — dados sensíveis (subset, regime mais rigoroso)
    if health:
        out["lgpd_sensitive"] = {
            "applies": True,
            "reason": "Dados de saúde são sensíveis (art. 11) — exige base legal específica",
        }
    else:
        out["lgpd_sensitive"] = {"applies": False, "reason": "Sem dados sensíveis declarados"}

    # GDPR — aplica se residency=EU OU se processa PII sem residency declarada
    # (cautela: pode haver usuário UE entre os consumers)
    if residency == "EU":
        out["gdpr"] = {
            "applies": True,
            "reason": "Dados residem na UE (residency=EU) — GDPR jurisdição direta",
        }
    elif pii and not residency:
        out["gdpr"] = {
            "applies": True,
            "reason": "Processa PII sem residency declarada — assuma GDPR por precaução até confirmar",
        }
    else:
        out["gdpr"] = {"applies": False, "reason": "Sem PII em jurisdição UE"}

    # HIPAA — saúde + US (caso raro mas explícito)
    if health and residency == "US":
        out["hipaa"] = {
            "applies": True,
            "reason": "Dados de saúde residindo nos EUA — HIPAA aplica diretamente",
        }
    else:
        out["hipaa"] = {"applies": False, "reason": "Sem saúde em jurisdição US"}

    # Marco Civil — qualquer acesso à internet aberta dispara
    if accesses_internet:
        out["marco_civil"] = {
            "applies": True,
            "reason": "Acessa internet aberta — registros de conexão e aplicação (art. 13-15) aplicam",
        }
    else:
        out["marco_civil"] = {"applies": False, "reason": "Não acessa internet aberta"}

    return out


# ─── Alertas automáticos ───────────────────────────────────────────


def compute_alerts(
    entry: dict,
    disclosure: Optional[dict],
    external_metadata: Optional[dict] = None,
) -> list[dict]:
    """Detecta inconsistências entre declaração e prática esperada.

    Retorna lista de {severity, code, message}:
    - severity: 'danger' | 'warning' | 'info'
    - code: identificador estável para regression test e i18n futuro
    - message: texto humano em PT-BR

    Lista ordenada: danger primeiro, depois warning, depois info.
    """
    out: list[dict] = []

    if disclosure is None:
        out.append({
            "severity": "warning",
            "code": "disclosure_missing",
            "message": "Capability Disclosure não declarada — comitê não consegue avaliar exposição.",
        })
        # Sem disclosure não há mais checks possíveis
        return out

    pii = disclosure.get("processes_pii") is True
    financial = disclosure.get("processes_financial") is True
    health = disclosure.get("processes_health") is True
    stores_input = disclosure.get("stores_input") is True
    retention = disclosure.get("storage_retention_days")
    calls_apis = disclosure.get("calls_external_apis") is True
    accesses_internet = disclosure.get("accesses_internet") is True
    apis_list = disclosure.get("external_apis_list") or []
    residency = disclosure.get("data_residency")
    trains = disclosure.get("trains_on_input") is True

    # DANGER: trains_on_input com PII/saúde sem consentimento claro
    if trains and (pii or health):
        out.append({
            "severity": "danger",
            "code": "trains_on_sensitive",
            "message": "Input vira training data com PII ou saúde — exige consentimento específico (LGPD art. 11 §2º).",
        })

    # DANGER: stores_input com PII e SEM retention
    if stores_input and pii and retention in (None, 0):
        out.append({
            "severity": "danger",
            "code": "pii_stored_without_retention",
            "message": "Persiste input com PII mas retention_days não declarado — retenção indefinida fere LGPD art. 16.",
        })

    # WARNING: retention > 365 dias com dados sensíveis
    if stores_input and (pii or financial or health) and isinstance(retention, int) and retention > 365:
        out.append({
            "severity": "warning",
            "code": "long_retention_sensitive",
            "message": f"Retenção de {retention} dias para dados sensíveis — justifique a finalidade ou reduza para o mínimo necessário.",
        })

    # WARNING: calls_external_apis sem lista declarada
    if calls_apis and not apis_list:
        out.append({
            "severity": "warning",
            "code": "external_apis_undeclared",
            "message": "Chama APIs externas mas lista de URLs não foi declarada — sem rastreabilidade para auditoria.",
        })

    # WARNING: PII sem residency declarada
    if pii and not residency:
        out.append({
            "severity": "warning",
            "code": "pii_without_residency",
            "message": "Processa PII sem soberania de dados declarada — defina BR/EU/US ou justifique 'global'.",
        })

    # WARNING: acessa internet aberta + dados regulados
    if accesses_internet and (pii or financial or health):
        out.append({
            "severity": "warning",
            "code": "internet_with_sensitive",
            "message": "Acessa internet aberta tratando dados regulados — risco de exfiltração; revise as queries permitidas.",
        })

    # WARNING: external_platform sem vendor ou contrato
    if entry.get("kind") == "external_platform":
        ext = external_metadata or {}
        if not ext.get("vendor"):
            out.append({
                "severity": "warning",
                "code": "external_platform_no_vendor",
                "message": "Plataforma externa sem vendor documentado — comitê não consegue avaliar DPA.",
            })
        elif not ext.get("contract_status"):
            out.append({
                "severity": "info",
                "code": "external_platform_no_contract",
                "message": f"Vendor '{ext.get('vendor')}' sem contract_status — confirme se há DPA assinado.",
            })

    # INFO: entry publicada há muito sem invocação recente (heurística — caller
    # passa entry.last_invoked_at se quiser este check; senão skip)
    last = entry.get("last_invoked_at") or entry.get("trust_last_invoked_at")
    if entry.get("status") == "published" and last:
        try:
            from datetime import datetime, timezone
            if isinstance(last, str):
                # Tenta parse ISO
                last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            else:
                last_dt = last
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            days_idle = (datetime.now(timezone.utc) - last_dt).days
            if days_idle > 60:
                out.append({
                    "severity": "info",
                    "code": "stale_entry",
                    "message": f"Última invocação há {days_idle} dias — entry candidata a depreciação se já não é usada.",
                })
        except Exception:
            pass  # last_invoked_at em formato inesperado — pula o check, não derruba

    # Ordena: danger > warning > info; estabilidade preservada dentro de cada nível
    severity_order = {"danger": 0, "warning": 1, "info": 2}
    out.sort(key=lambda a: severity_order.get(a["severity"], 99))
    return out
