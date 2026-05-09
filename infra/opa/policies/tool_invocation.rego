# ════════════════════════════════════════════════════════════════
# Política: gate de invocação de tools (MCP / function calling).
#
# Schema esperado de input:
#   {
#     "tool": {
#       "name": str,
#       "sensitivity": "low"|"medium"|"high",
#       "requires_trusted_context": bool
#     },
#     "user":    {"role": "viewer"|"operator"|"admin"},
#     "context": {"is_trusted": bool}
#   }
#
# Avalia em: POST /v1/data/tool_invocation/allow → {"result": true|false}
#            POST /v1/data/tool_invocation/reason → {"result": "..."}
# ════════════════════════════════════════════════════════════════
package tool_invocation

import rego.v1

default allow := false
default reason := "denied"

# Tool low: qualquer usuário (até viewer)
allow if {
    sensitivity == "low"
}

# Tool medium: operator ou admin
allow if {
    sensitivity == "medium"
    input.user.role in {"operator", "admin"}
}

# Tool high sem trusted_context: admin é suficiente
allow if {
    sensitivity == "high"
    input.user.role == "admin"
    not input.tool.requires_trusted_context
}

# Tool high COM trusted_context: admin + contexto verificadamente confiável
allow if {
    sensitivity == "high"
    input.user.role == "admin"
    input.tool.requires_trusted_context == true
    input.context.is_trusted == true
}

# Default sensitivity = "low" se não informado (ferramentas legacy não preenchem).
sensitivity := s if {
    s := input.tool.sensitivity
}
sensitivity := "low" if {
    not input.tool.sensitivity
}

# Razões human-readable para o audit log.
reason := "low_anyone_allowed" if {
    sensitivity == "low"
    allow
}
reason := "medium_role_ok" if {
    sensitivity == "medium"
    allow
}
reason := "high_admin_ok" if {
    sensitivity == "high"
    allow
}
reason := "insufficient_role" if {
    sensitivity != "low"
    not allow
    not blocked_by_trusted_context
}
reason := "missing_trusted_context" if {
    blocked_by_trusted_context
}

blocked_by_trusted_context if {
    sensitivity == "high"
    input.user.role == "admin"
    input.tool.requires_trusted_context == true
    not input.context.is_trusted
}
