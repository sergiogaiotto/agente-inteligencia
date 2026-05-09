# ════════════════════════════════════════════════════════════════
# Política: PolicyCheck do FSM (substituto do stub legacy).
#
# Schema esperado de input:
#   {
#     "prompt_injection": {"score": float},   // score do prompt_guard 0..1
#     "rate_limit":       {"exceeded": bool},
#     "user":             {"status": "active"|"inactive"|"suspended"}
#   }
#
# Avalia em: POST /v1/data/interaction/allow → {"result": true|false}
#            POST /v1/data/interaction/reasons → {"result": ["..."]}
# ════════════════════════════════════════════════════════════════
package interaction

import rego.v1

default allow := false

# `reasons` é uma partial set rule (`contains ... if ...`). Em Rego v1
# essas regras NÃO podem ter `default` — set vazio é implícito quando
# nenhuma cláusula casa.

# allow se nenhum motivo de bloqueio se aplica
allow if {
    not blocked_by_prompt_injection
    not blocked_by_rate_limit
    not blocked_by_user_inactive
}

# Threshold deve casar com prompt_guard_block_threshold do app.
blocked_by_prompt_injection if {
    input.prompt_injection.score >= 0.7
}

blocked_by_rate_limit if {
    input.rate_limit.exceeded == true
}

blocked_by_user_inactive if {
    input.user.status == "inactive"
}
blocked_by_user_inactive if {
    input.user.status == "suspended"
}

# Coleção de motivos — usado pelo PEP para enriquecer mensagem de refuse.
reasons contains "prompt_injection_blocked" if blocked_by_prompt_injection
reasons contains "rate_limited" if blocked_by_rate_limit
reasons contains "user_inactive" if blocked_by_user_inactive
