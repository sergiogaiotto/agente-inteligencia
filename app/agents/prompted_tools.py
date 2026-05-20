"""Function calling 'via prompt' — fallback para modelos sem tools nativo.

Quando o modelo do agent não suporta `tools` parameter no API
(maritaca/sabia-3, ollama/gemma, modelos desconhecidos), a plataforma
cai aqui em vez de perder as tools silenciosamente.

**Estratégia**:
1. `build_prompted_tools_system(tools)` — injeta os schemas das tools
   no system prompt, junto com instruções claras sobre o formato
   esperado de tool_call (XML envolvendo JSON).
2. Modelo responde texto livre que PODE conter `<tool_call>{...}</tool_call>`.
3. `parse_tool_calls(text)` extrai esses blocos com regex tolerante
   (aceita aspas suaves, whitespace, JSON malformado dentro do limite
   conhecido — descarta o que não bate).
4. Caller executa as tool calls via MCP e injeta resultado de volta
   como mensagem (estilo `ToolMessage` do LangChain).

**Limitações vs nativo**:
- ~20-30% mais tokens (instruções + schemas no prompt).
- ~5-10% de JSON malformado em primeiras tentativas — parser tolera,
  mas se a tool_call não puder ser parseada, é ignorada e o modelo
  é reorientado na próxima rodada.
- Sem streaming de tool_calls — modelo precisa gerar resposta inteira
  antes de saber quais tools chamar.

**Quando NÃO usar**:
- Modelo tem `native_tools=True` em llm_capabilities — use bind_tools().
- Modelo tem `prompted_ok=False` (gemma 2B, etc.) — caia em texto puro
  (sem tools) + audit warning. Não vale tentar prompted em modelo
  que não segue instrução.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# Marker XML envolvendo JSON — escolhido para ser:
# - Distinguível de texto normal (improvável de aparecer fora de contexto)
# - Fácil de parsear (regex simples)
# - Robusto a aspas malformadas (delimitador externo é XML, não JSON)
_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*(\{.*?\})\s*</tool_call>",
    re.DOTALL,
)


_PROMPT_TEMPLATE = """\
Você tem acesso às seguintes ferramentas (tools) que pode invocar quando \
necessário para responder ao usuário:

{tools_block}

Para invocar uma ferramenta, gere um bloco no formato exato abaixo na sua \
resposta. Você pode invocar VÁRIAS ferramentas em sequência (um bloco por tool):

<tool_call>{{"name": "nome_da_tool", "arguments": {{"arg1": "valor1", ...}}}}</tool_call>

REGRAS IMPORTANTES:
- O JSON dentro de <tool_call> precisa ser válido (aspas duplas, sem trailing \
comma, sem comentários).
- Use o nome EXATO da tool como listado acima.
- Os argumentos devem seguir o schema declarado de cada tool.
- Se você não precisa chamar nenhuma tool, responda normalmente em texto livre \
sem blocos <tool_call>.
- Depois que as ferramentas retornarem resultado, use-o para compor a resposta \
final ao usuário.
"""


def build_prompted_tools_system(tools: list[dict]) -> str:
    """Constrói o trecho de system prompt que ensina o modelo a invocar tools.

    `tools` segue o mesmo formato OpenAI (lista de dicts com `type='function'`
    e `function: {name, description, parameters}`). É o mesmo formato que o
    engine já constrói via `build_openai_tools()` para nativo — reusamos.

    Retorna string para ser concatenada ao system_prompt principal do agent.
    """
    if not tools:
        return ""
    blocks = []
    for t in tools:
        fn = t.get("function") or t  # tolera shape simplificado
        name = fn.get("name", "")
        desc = fn.get("description", "").strip()
        params = fn.get("parameters") or {}
        # Schema compacto: nome + descrição + JSON-schema dos params
        try:
            params_json = json.dumps(params, ensure_ascii=False, indent=2)
        except Exception:
            params_json = "{}"
        blocks.append(
            f"### {name}\n"
            f"{desc}\n\n"
            f"Argumentos (JSON Schema):\n```json\n{params_json}\n```"
        )
    tools_block = "\n\n".join(blocks)
    return _PROMPT_TEMPLATE.format(tools_block=tools_block)


def parse_tool_calls(text: str) -> list[dict]:
    """Extrai blocos <tool_call>{...}</tool_call> do texto da resposta.

    Parser tolerante: blocos com JSON inválido são DESCARTADOS (não levantam
    exception) — caller pode re-prompt o modelo se nenhuma tool válida foi
    extraída e ele esperava uma.

    Returns: lista de dicts {name, arguments}. Vazia se nenhum bloco
    encontrado ou todos inválidos.
    """
    if not text or not isinstance(text, str):
        return []

    out: list[dict] = []
    for m in _TOOL_CALL_RE.finditer(text):
        raw = m.group(1)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            # Tentativa 2: aspas simples → duplas (pragmatismo)
            try:
                parsed = json.loads(raw.replace("'", '"'))
            except json.JSONDecodeError:
                logger.warning(
                    f"prompted_tools: tool_call malformado descartado: {raw[:120]!r}"
                )
                continue
        if not isinstance(parsed, dict):
            continue
        name = parsed.get("name")
        if not name or not isinstance(name, str):
            continue
        args = parsed.get("arguments") or parsed.get("args") or {}
        if not isinstance(args, dict):
            # Argumentos como string? Tenta parsear se parece JSON
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"raw": args}
            else:
                args = {}
        out.append({"name": name.strip(), "arguments": args})
    return out


def strip_tool_calls(text: str) -> str:
    """Remove os blocos <tool_call> do texto, deixando só a parte 'humana'.

    Útil para extrair o conteúdo que vai para o usuário quando o modelo
    misturou raciocínio + chamadas no mesmo turno (acontece com modelos
    menos rigorosos).
    """
    if not text:
        return ""
    return _TOOL_CALL_RE.sub("", text).strip()


def format_tool_result_message(tool_name: str, result: Any) -> str:
    """Formata o resultado de uma tool chamada em mensagem para o modelo.

    Estilo: tag XML simples envolvendo JSON do resultado. Modelo aprende
    rápido a interpretar (visto também em treinamento de muitos LLMs).
    """
    try:
        result_str = (
            json.dumps(result, ensure_ascii=False, indent=2)
            if not isinstance(result, str)
            else result
        )
    except Exception:
        result_str = str(result)
    return f"<tool_result tool=\"{tool_name}\">\n{result_str}\n</tool_result>"
