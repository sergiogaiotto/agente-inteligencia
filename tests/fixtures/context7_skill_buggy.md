---
id: urn:skill:geral:subagent:context7-design-pattern
version: 0.1.0
kind: subagent
owner: equipe-ia
stability: alpha
---

# Context7 Design Pattern Generator

## Purpose
Gerar descrições detalhadas, diagramas conceituais e snippets de código para o padrão de design solicitado, **apenas** utilizando a base de conhecimento da ferramenta **Context 7 MCP Server**.
**Não** realiza pesquisa fora desse binding, nem cria conteúdo que não seja derivado da resposta do Context7.

## Activation Criteria
Este skill deve ser ativado quando:
* O usuário solicitar a criação ou explicação de um padrão de design de software (ex.: "Factory Method", "Observer", "Singleton", etc.).
* O pedido contiver a intenção explícita de usar o Context7 como fonte de documentação/código.

## Inputs

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "DesignPatternRequest",
  "type": "object",
  "properties": {
    "design_pattern_request": {
      "type": "string",
      "description": "Nome do padrão de design solicitado"
    }
  },
  "required": ["design_pattern_request"],
  "additionalProperties": false
}
```

## Workflow
1. **Chame** a tool `Context 7 MCP Server` com `operation=search` e `query=<design_pattern_request>` **antes** de gerar a resposta.
2. **Avalie** a relevância da resposta retornada usando o critério interno da skill (ex.: presença do nome do padrão, exemplos de código, diagramas).
3. **Formate** o conteúdo retornado em um objeto JSON contendo pattern_name, description, diagram_url, code_example.
4. **Retorne** o objeto formatado como saída do skill.

## Tool Bindings
- `481c5fa3-36bc-4d05-97ff-d502d93521ff` (Context 7 MCP Server) — Plataforma Context7 para documentação e código atualizado de qualquer prompt, disponível como MCP Server.

## Output Contract

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "DesignPatternResult",
  "type": "object",
  "properties": {
    "pattern_name": {"type": "string"},
    "description": {"type": "string"},
    "diagram_url": {"type": "string", "format": "uri"},
    "code_example": {"type": "string"}
  },
  "required": ["pattern_name", "description", "code_example"],
  "additionalProperties": false
}
```

## Failure Modes
| Falha | Condição | Ação |
|---|---|---|
| Binding indisponível | Erro de rede ou timeout | Retornar erro `binding_unavailable` |
| Resposta vazia | Nenhum conteúdo do padrão | Retornar erro `no_relevant_content` |

## Evidence Policy
A única fonte autorizada é o binding **Context 7 MCP Server** declarado em ## Tool Bindings. Todo o conteúdo gerado deve derivar diretamente da resposta obtida por esse binding; não há outros knowledge sources ou thresholds adicionais.

## Guardrails
* **Conteúdo proibido:** informações que violem direitos autorais, PII, políticas de uso.
* **Limitações técnicas:** Não gerar código malicioso, vulnerável.

## Examples

### Exemplo 1 — Geração de Factory Method em Python
**Entrada:**
```json
{"design_pattern_request": "Factory Method em Python"}
```

**Chamada à tool:** `Context 7 MCP Server` `operation=search` `query=Factory Method em Python`

**Resposta do binding:** Retornou descrição do padrão Factory Method, diagramas UML e exemplo Python.

**Saída final:**
```json
{"pattern_name": "Factory Method", "description": "...", "code_example": "..."}
```

### Exemplo 2 — Explicação do Singleton em Java
**Entrada:**
```json
{"design_pattern_request": "Singleton em Java"}
```

**Chamada à tool:** `Context 7 MCP Server` `operation=search` `query=Singleton em Java`

**Resposta do binding:** Forneceu definição, thread-safety, e snippet Java.

**Saída final:**
```json
{"pattern_name": "Singleton", "description": "...", "code_example": "..."}
```

## Execution Profile
mode: fast
reflection: off
evidence: skip

## Output Shape

```yaml
length_preset: unbounded
```
