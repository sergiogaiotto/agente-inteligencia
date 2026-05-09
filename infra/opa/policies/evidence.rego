# ════════════════════════════════════════════════════════════════
# Política: acesso a evidência por nível de confidencialidade.
#
# Schema esperado de input:
#   {
#     "user":     {"clearance": "public"|"internal"|"confidential"|"secret"},
#     "evidence": {"confidentiality": "public"|"internal"|"confidential"|"secret"}
#   }
#
# Status nesta Onda: política existe mas NÃO é chamada pelo PEP atual.
# `users.clearance` ainda não é coluna do banco. Quando for adicionada
# (iteração futura), basta wirar a chamada em runtime.py:Retriever._hydrate.
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

# Hierarquia ordinal: maior número = mais sensível.
rank := {
    "public":       0,
    "internal":     1,
    "confidential": 2,
    "secret":       3,
}
