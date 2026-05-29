---
id: urn:skill:geral:subagent:evaluar-design-pattern
version: 0.1.0
kind: subagent
owner: equipe-ia
stability: alpha
---

# Avaliar Design Pattern com Context 7 MCP

## Purpose
Avaliar a adequação de um **design pattern** a um cenário de software específico, apresentando pontos fortes, limitações e uma recomendação de uso.

## Activation Criteria
Este skill deve ser selecionado quando:
- O usuário solicita a avaliação ou comparação de um design pattern (ex.: "Me ajude a entender se o Singleton é adequado para meu módulo de logging");
- O input contém o nome do pattern e, opcionalmente, uma breve descrição do contexto de aplicação.

## Inputs

```json
{
  "type": "object",
  "properties": {
    "pattern_name": {"type": "string"},
    "context_description": {"type": "string", "default": ""}
  },
  "required": ["pattern_name"]
}
```

## Workflow
1. **Chame** a tool `Context 7 MCP Server` para obter a documentação oficial e boas‑práticas do pattern indicado, usando o valor de `pattern_name` como consulta.
2. **Analise** o conteúdo retornado, extraindo os princípios, casos de uso típicos e advertências.
3. **Correlacione** as informações do binding com o `context_description` fornecido pelo usuário.
4. **Construa** a saída conforme o contrato definido na seção **Output Contract** e retorne ao usuário.

## Tool Bindings
- `481c5fa3-36bc-4d05-97ff-d502d93521ff` (Context 7 MCP Server) — Plataforma Context7 para documentação e código atualizado de qualquer prompt, disponível como MCP Server.
  *Operações disponíveis*: **nenhuma operação declarada**. O passo 1 do workflow simplesmente **aciona** o binding; a plataforma interpreta a consulta implícita baseada no parâmetro `pattern_name`.

## Output Contract

```json
{
  "type": "object",
  "properties": {
    "pattern_name": {"type": "string"},
    "suitability_score": {"type": "number"},
    "strengths": {"type": "array"},
    "weaknesses": {"type": "array"},
    "recommendation": {"type": "string"}
  },
  "required": ["pattern_name", "suitability_score", "strengths", "weaknesses", "recommendation"]
}
```

## Failure Modes
| Falha | Ação |
|---|---|
| BindingError | Mensagem amigável |
| InputValidationError | Solicitar correção |

## Evidence Policy
_A única fonte autorizada é o binding **Context 7 MCP Server** declarado em ## Tool Bindings._
Não há thresholds de relevância configurados explicitamente.

## Guardrails
- Não gerar código executável.
- Não solicitar PII.

## Examples

### Exemplo 1 — Avaliar Singleton
**Entrada:** `{"pattern_name": "Singleton"}`
**Ação no binding:** `Context 7 MCP Server` – consulta implícita ao repositório de documentação.
**Resposta do binding:** "Singleton garante única instância..."
**Saída final:** `{"pattern_name": "Singleton", "suitability_score": 0.78, "strengths": [...], "weaknesses": [...], "recommendation": "..."}`

## Execution Profile
mode: rigorous
reflection: on
evidence: required
