---
id: urn:skill:geral:subagent:context7-document-fetch
version: 0.1.0
kind: subagent
owner: equipe-ia
stability: alpha
---

# Context7 Documentation Fetcher

## Purpose
Obter a documentação mais recente e exemplos de código da plataforma **Context 7** a partir de uma consulta textual fornecida pelo usuário.
**Não** gera conteúdo próprio nem interpreta código fora do escopo da documentação oficial.

## Activation Criteria
Este skill deve ser selecionado quando:
- O usuário solicitar "documentação", "exemplo", "referência" ou "código" relacionado ao **Context 7**.
- A entrada contiver um campo `query` descrevendo a necessidade de informação técnica.

## Inputs

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "Context7QueryInput",
  "type": "object",
  "properties": {
    "query": {"type": "string"}
  },
  "required": ["query"],
  "additionalProperties": false
}
```

## Workflow
1. **Chame** a tool `Context 7 MCP Server` com o parâmetro `query=<valor do campo query>` para buscar a documentação correspondente.
2. **Avalie** a resposta: se o conteúdo retornado possuir relevância suficiente (conforme o `min_relevance` definido na **Evidence Policy**), formate-o para o output.
3. **Retorne** o resultado estruturado conforme o **Output Contract**.
4. Caso a resposta seja insuficiente ou ocorra erro na chamada, siga o fluxo descrito em **Failure Modes**.

## Tool Bindings
- `481c5fa3-36bc-4d05-97ff-d502d93521ff` (Context 7 MCP Server) — Plataforma Context7 para documentação e código atualizado de qualquer prompt, disponível como MCP Server.

## Output Contract

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "Context7DocumentationOutput",
  "type": "object",
  "properties": {
    "documentation": {"type": "string"},
    "source_id": {
      "type": "string",
      "enum": ["481c5fa3-36bc-4d05-97ff-d502d93521ff"]
    }
  },
  "required": ["documentation", "source_id"],
  "additionalProperties": false
}
```

## Failure Modes
| Falha | Detecção | Ação corretiva |
|---|---|---|
| Timeout | Exceção lançada | Mensagem amigável de indisponibilidade. |
| Resposta vazia | Score abaixo de min_relevance | Sugerir reformulação. |
| Formato inesperado | Falha de parse JSON | Mensagem genérica de falha. |

## Evidence Policy
A única fonte autorizada é o binding **Context 7 MCP Server** declarado em **Tool Bindings**.
Qualquer informação retornada deve ser considerada válida somente se atender ao `min_relevance` definido na política de evidência da plataforma.

## Guardrails
- **Privacidade:** Não armazenar nem retransmitir PII.
- **Conteúdo sensível:** Bloquear segredos.
- **Juridição:** Respeitar direitos autorais.

## Examples

### Exemplo 1 — Busca de referência de API
**Entrada:** `{"query": "Como criar um workspace via API?"}`
**Chamada à tool:** `Context 7 MCP Server` query=`Como criar um workspace via API?`
**Resposta do binding:** `{"documentation": "POST /api/v1/workspaces", "relevance_score": 0.92}`
**Saída final:** `{"documentation": "POST /api/v1/workspaces", "source_id": "481c5fa3-..."}`

### Exemplo 2 — Código de autenticação
**Entrada:** `{"query": "Exemplo Python para autenticar"}`
**Chamada à tool:** `Context 7 MCP Server` query=`Exemplo Python para autenticar`
**Resposta do binding:** `{"documentation": "import requests; headers={'Authorization': f'Bearer {TOKEN}'}", "relevance_score": 0.88}`
**Saída final:** `{"documentation": "import requests; ...", "source_id": "481c5fa3-..."}`

## Execution Profile
mode: fast
reflection: off
evidence: skip

## Output Shape

```yaml
length_preset: unbounded
```
