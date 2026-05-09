#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════
# Scan rápido por padrões comuns de API keys em arquivos versionados.
# Onda 4c — secrets management.
#
# Uso:
#   ./infra/scripts/check-secrets-leak.sh           # arquivos rastreados pelo git
#   ./infra/scripts/check-secrets-leak.sh --staged  # apenas arquivos staged
#
# Patterns detectados (high-confidence — prefixos distintivos):
#   sk-proj-...           OpenAI project keys
#   sk-ant-...            Anthropic
#   sk-litellm-...        LiteLLM master keys (Onda 4b)
#   pk-lf-... / sk-lf-... LangFuse public/secret
#   xoxb-...              Slack bot tokens
#   ghp_/gho_/ghu_...     GitHub tokens
#   AKIA[0-9A-Z]{16}      AWS access keys
#
# Não detecta (intencionalmente, falsos positivos demais):
#   - Senhas Postgres genéricas (POSTGRES_PASSWORD=...) — recomenda-se grep manual
#   - Chaves Azure (string aleatória sem prefixo distintivo)
#
# Exit code: 0 limpo, 1 algo encontrado.
# ════════════════════════════════════════════════════════════════

set -e
cd "$(dirname "$0")/../.."

MODE="${1:-tracked}"

case "$MODE" in
  --staged)
    FILES=$(git diff --cached --name-only --diff-filter=ACM 2>/dev/null || true)
    SCOPE="arquivos staged"
    ;;
  tracked|"")
    FILES=$(git ls-files 2>/dev/null || true)
    SCOPE="arquivos versionados"
    ;;
  *)
    echo "Uso: $0 [--staged]"
    exit 2
    ;;
esac

if [[ -z "$FILES" ]]; then
  echo "Nenhum $SCOPE para escanear."
  exit 0
fi

# Exclui binários e o próprio script (que contém os padrões como string).
FILES=$(echo "$FILES" | grep -vE '\.(png|jpg|jpeg|gif|pdf|zip|gz|tar|whl|so|dll|exe|webp|ico)$' | grep -v "check-secrets-leak.sh" || true)

PATTERNS=(
  'sk-proj-[A-Za-z0-9_-]{30,}'
  'sk-ant-[A-Za-z0-9_-]{40,}'
  'sk-litellm-[a-f0-9]{40,}'
  'pk-lf-[a-f0-9-]{30,}'
  'sk-lf-[a-f0-9-]{30,}'
  'xoxb-[A-Za-z0-9-]{20,}'
  'gh[pousr]_[A-Za-z0-9]{30,}'
  'AKIA[0-9A-Z]{16}'
)

found=0
for pat in "${PATTERNS[@]}"; do
  hits=$(echo "$FILES" | xargs -I{} grep -EHn -- "$pat" {} 2>/dev/null | head -10 || true)
  if [[ -n "$hits" ]]; then
    echo "🚨 padrão: $pat"
    echo "$hits"
    echo
    found=1
  fi
done

if [[ $found -eq 0 ]]; then
  echo "✅ Nenhum padrão de chave detectado em $SCOPE."
  echo "   (Patterns sem prefixo distintivo — Azure, postgres password — não são cobertos.)"
  exit 0
else
  echo "❌ Possíveis chaves expostas. Revise os matches acima."
  echo "   - Se vazaram: rotacione AGORA no painel do provedor."
  echo "   - Se for falso positivo: ajuste padrões em check-secrets-leak.sh."
  exit 1
fi
