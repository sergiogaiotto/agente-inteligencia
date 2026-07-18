# ════════════════════════════════════════════════════════════════
# Política: acesso a evidência por nível de confidencialidade.
#
# Schema esperado de input:
#   {
#     "user":     {"clearance": "public"|"internal"|"confidential"|"restricted"},
#     "evidence": {"confidentiality": "public"|"internal"|"confidential"|"restricted"}
#   }
#   ("secret" é aceito como alias de "restricted" no rank abaixo.)
#
# Status (64.0.0): ATIVA. `users.clearance` é coluna do banco (default 'internal').
# Chamada por app.core.opa_policies.evidence_allows via app.evidence.runtime.
# Retriever._acl_filter, gated pela flag `evidence_acl_enabled` (default OFF).
#
# Avalia em: POST /v1/data/evidence/allow → {"result": true|false}
# ════════════════════════════════════════════════════════════════
package evidence

import rego.v1

default allow := false

# Permite se clearance do usuário >= confidencialidade da evidência
allow if {
    rank[input.user.clearance] >= rank[input.evidence.confidentiality]
}

# Hierarquia ordinal: maior número = mais sensível. "restricted" é o rótulo do
# 4º nível usado na UI de Bases de Conhecimento; "secret" é o sinônimo padrão —
# ambos rankeiam no topo para o "no read up" funcionar com os dois vocabulários.
rank := {
    "public":       0,
    "internal":     1,
    "confidential": 2,
    "restricted":   3,
    "secret":       3,
}
