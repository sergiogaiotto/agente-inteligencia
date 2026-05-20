"""Endpoint de IA Help — assistente contextual para o Guia dos Módulos.

POST /api/v1/help/ask
  body: {module_id, module_label, module_section, question, history?: list}
  resp: {answer, model, usage}

Estratégia:
- Carrega o conteúdo do módulo de `app/static/js/module-guide.js` no startup
  (parse mínimo do `window.MODULE_GUIDE = [...]`) para usar como contexto.
- Prompt template injeta: módulo, fundamento, ativar, usar, history, question.
- Chama `get_provider("azure")` (via gateway se ativo, senão direto).
- max_tokens cap pra controlar custo (~$0.001/pergunta).
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.llm_providers import get_provider
from app.core.otel import get_tracer

logger = logging.getLogger(__name__)
_tracer = get_tracer(__name__)

router = APIRouter(prefix="/api/v1/help", tags=["help"])

MAX_TOKENS = 600
MAX_HISTORY = 5  # turnos passados a manter no contexto

# Stripper simples de HTML para enviar texto ao LLM.
_HTML_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _strip_html(s: str) -> str:
    if not s:
        return ""
    no_tags = _HTML_TAG.sub(" ", s)
    return _WS.sub(" ", no_tags).strip()


# Cache do parse do module-guide.js (carrega 1x por processo).
_MODULE_CACHE: dict[str, dict] | None = None


def _load_module_guide() -> dict[str, dict]:
    """Parse mínimo do array MODULE_GUIDE. Procura por entries `{id, section,
    label, fundamento, aplicacao, ativar, usar}` via regex robusta a backticks
    multi-linha. Não depende de execução JS.

    Cacheia em memória — recarrega só com restart.
    """
    global _MODULE_CACHE
    if _MODULE_CACHE is not None:
        return _MODULE_CACHE

    path = Path(__file__).resolve().parent.parent / "static" / "js" / "module-guide.js"
    if not path.exists():
        logger.warning(f"module-guide.js não encontrado em {path}")
        _MODULE_CACHE = {}
        return _MODULE_CACHE

    src = path.read_text(encoding="utf-8")
    # Cada entry começa com `id: 'xxx'`. Pegamos blocos delimitados por `{` ... `},`
    # contendo id/section/label/fundamento/etc. Como Python regex não tem
    # parser JS, usamos heurística: encontra `id: '<x>'` e captura próximos
    # 5 campos via regex específica para template literals (backticks).
    entries: dict[str, dict] = {}
    for m in re.finditer(r"\{\s*id:\s*['\"]([^'\"]+)['\"]", src):
        start = m.start()
        # Encontra o `},` que fecha este objeto no nível raiz do array.
        # Heurística: balanceamento de `{}` ignorando dentro de strings/backticks.
        depth = 0
        in_bt = False  # backtick template literal
        in_str = False
        sc = None
        i = start
        while i < len(src):
            c = src[i]
            if in_bt:
                if c == "`":
                    in_bt = False
                i += 1
                continue
            if in_str:
                if c == "\\":
                    i += 2
                    continue
                if c == sc:
                    in_str = False
                    sc = None
                i += 1
                continue
            if c == "`":
                in_bt = True
                i += 1
                continue
            if c in ("'", '"'):
                in_str = True
                sc = c
                i += 1
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        block = src[start:i + 1]

        def _grab(field: str) -> str:
            # Captura `field: \`...\`` permitindo backticks multi-linha.
            mm = re.search(rf"{field}\s*:\s*`((?:\\`|[^`])*)`", block, re.DOTALL)
            if mm:
                return mm.group(1)
            # Fallback string com aspas simples ou duplas
            mm = re.search(rf"{field}\s*:\s*['\"]((?:\\.|[^'\"])*)['\"]", block, re.DOTALL)
            return mm.group(1) if mm else ""

        eid = m.group(1)
        entries[eid] = {
            "id": eid,
            "section": _grab("section"),
            "label": _grab("label"),
            "fundamento": _strip_html(_grab("fundamento")),
            "aplicacao": _strip_html(_grab("aplicacao")),
            "ativar": _strip_html(_grab("ativar")),
            "usar": _strip_html(_grab("usar")),
        }

    _MODULE_CACHE = entries
    logger.info(f"module-guide carregado: {len(entries)} módulos")
    return entries


class HelpAskRequest(BaseModel):
    module_id: str = Field(..., description="id do módulo (ex: 's14', 'onda4a')")
    module_label: str | None = None
    module_section: str | None = None
    question: str = Field(..., min_length=1, max_length=2000)
    history: list[dict] | None = Field(default=None, description="Turnos anteriores [{role, content}]")


class HelpAskContextRequest(BaseModel):
    """Versão legacy (Onda 0): recebe contexto plano via {what, foundation, usage}.
    Mantida para retrocompat de páginas ainda não migradas para o schema V2.
    """
    title: str = Field(..., max_length=200)
    section: str = Field(..., max_length=200)
    what: str = Field(default="", max_length=4000)
    foundation: str = Field(default="", max_length=4000)
    usage: str = Field(default="", max_length=4000)
    question: str = Field(..., min_length=1, max_length=2000)
    history: list[dict] | None = None


# ─── V2: contexto rico do help-content.js (PR 5 — Guia Interativo) ─────


class HelpSectionItem(BaseModel):
    """Item dentro de uma section (campo, caso de uso ou pegadinha)."""
    name: str | None = None
    title: str | None = None
    body: str = ""
    severity: str | None = None  # info | warning | danger (só pegadinhas)
    required: bool | None = None
    default: str | None = None
    options: list[str] | None = None
    example: str | None = None


class HelpSection(BaseModel):
    """Section tipada — espelha o schema do app/static/js/help-content.js."""
    kind: str  # concept | fundamentos | campos | casos_de_uso | exemplo | pegadinhas
    title: str
    body: str | None = None
    items: list[HelpSectionItem] | None = None


class HelpAskContextV2Request(BaseModel):
    """Versão V2 — recebe o schema rico do help-content.js (sections tipadas).

    Permite que o LLM use contexto granular: 'O usuário está perguntando
    sobre o campo X' → o assistente acha o campo X em sections[campos].items[]
    e responde com base no body+example daquele campo específico.
    """
    title: str = Field(..., max_length=200)
    summary: str = Field(default="", max_length=500)
    sections: list[HelpSection] = Field(default_factory=list)
    related: list[str] | None = None
    question: str = Field(..., min_length=1, max_length=2000)
    history: list[dict] | None = None


class HelpAskResponse(BaseModel):
    answer: str
    model: str
    usage: dict[str, Any]


@router.post("/ask", response_model=HelpAskResponse)
async def ask_help(req: HelpAskRequest) -> HelpAskResponse:
    """Responde uma pergunta contextual sobre o módulo via LLM.

    Robusto:
    - Módulo desconhecido → 404.
    - LLM falha → 503 com erro explícito (não derruba o app).
    """
    with _tracer.start_as_current_span("help.ask") as span:
        span.set_attribute("module.id", req.module_id)
        span.set_attribute("question.length", len(req.question))

        modules = _load_module_guide()
        ctx = modules.get(req.module_id)
        if ctx is None:
            raise HTTPException(404, f"Módulo '{req.module_id}' desconhecido.")

        system_prompt = (
            "Você é um assistente técnico do produto Maestro, "
            "uma plataforma multi-agente. Responda perguntas do usuário sobre o "
            "MÓDULO específico que ele está consultando. Use as informações do "
            "CONTEXTO abaixo como verdade canônica. Seja direto e prático.\n\n"
            "REGRAS:\n"
            "- Se não souber, diga que não sabe — não invente comandos ou flags.\n"
            "- Se a pergunta sair do escopo do módulo, redirecione gentilmente.\n"
            "- Use blocos de código (```bash) para comandos.\n"
            "- Responda em português do Brasil. Tom direto, sem floreio.\n"
            "- Limite resposta a ~250 palavras."
        )

        ctx_text = (
            f"=== MÓDULO: {ctx.get('section', '')} — {ctx.get('label', '')} ===\n\n"
            f"FUNDAMENTO:\n{ctx.get('fundamento', '')}\n\n"
            f"APLICAÇÃO:\n{ctx.get('aplicacao', '')}\n\n"
            f"COMO ATIVAR:\n{ctx.get('ativar', '')}\n\n"
            f"COMO USAR:\n{ctx.get('usar', '')}"
        )

        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": ctx_text + "\n\n=== PERGUNTA DO USUÁRIO ===\n" + req.question},
        ]

        # Adiciona histórico recente (máx MAX_HISTORY turnos prévios) entre o
        # contexto e a pergunta atual.
        if req.history:
            tail = req.history[-MAX_HISTORY:]
            # Insere antes da última mensagem (que é a pergunta atual)
            messages = [messages[0]] + tail + [messages[-1]]

        try:
            provider = get_provider("azure")
            resp = await provider.generate(messages, max_tokens=MAX_TOKENS)
            answer = (resp.get("content") or "").strip()
            if not answer:
                answer = "(O modelo não retornou resposta. Tente reformular a pergunta.)"
            span.set_attribute("response.length", len(answer))
            return HelpAskResponse(
                answer=answer,
                model=resp.get("model", "azure"),
                usage=resp.get("usage", {}) or {},
            )
        except Exception as e:
            logger.warning(f"help.ask falhou: {type(e).__name__}: {e}")
            raise HTTPException(503, f"Assistente IA indisponível: {type(e).__name__}: {str(e)[:160]}")


# ─── Variante para "Ajuda desta página" ─────────────────────────
# Recebe o contexto direto do front (helpContent já tem os dados estruturados
# em base.html). Não depende de module-guide.js.

_PAGE_SYSTEM_PROMPT = (
    "Você é um assistente técnico do produto Maestro. "
    "O usuário está navegando em uma PÁGINA específica e quer ajuda contextual "
    "para usá-la. Use o CONTEXTO DA PÁGINA abaixo como verdade canônica.\n\n"
    "REGRAS:\n"
    "- Foco no fluxo da página (criar/editar/executar). Nada genérico.\n"
    "- Se a pergunta for sobre tela/elemento/botão da página, descreva em 1-2 passos diretos.\n"
    "- Se sair do escopo da página, redirecione gentilmente.\n"
    "- Use blocos ```bash``` para comandos curl quando relevante.\n"
    "- Português do Brasil. Tom direto. Limite ~250 palavras."
)


@router.post("/ask-context", response_model=HelpAskResponse)
async def ask_help_context(req: HelpAskContextRequest) -> HelpAskResponse:
    """Variante context-driven do help — recebe os campos diretamente.

    Usado pelo drawer "Ajuda desta página" que tem os dados em helpContent
    no template (não em module-guide.js).
    """
    with _tracer.start_as_current_span("help.ask_context") as span:
        span.set_attribute("page.title", req.title)
        span.set_attribute("page.section", req.section)
        span.set_attribute("question.length", len(req.question))

        # Strip de qualquer HTML que tenha vindo (front usa textContent, mas defesa)
        what_t = _strip_html(req.what)
        foundation_t = _strip_html(req.foundation)
        usage_t = _strip_html(req.usage)

        ctx_text = (
            f"=== PÁGINA: {req.title} — {req.section} ===\n\n"
            f"O QUE É:\n{what_t}\n\n"
            f"FUNDAMENTO (especificação):\n{foundation_t}\n\n"
            f"COMO USAR:\n{usage_t}"
        )

        messages: list[dict] = [
            {"role": "system", "content": _PAGE_SYSTEM_PROMPT},
            {"role": "user", "content": ctx_text + "\n\n=== PERGUNTA DO USUÁRIO ===\n" + req.question},
        ]
        if req.history:
            tail = req.history[-MAX_HISTORY:]
            messages = [messages[0]] + tail + [messages[-1]]

        try:
            provider = get_provider("azure")
            resp = await provider.generate(messages, max_tokens=MAX_TOKENS)
            answer = (resp.get("content") or "").strip()
            if not answer:
                answer = "(O modelo não retornou resposta. Tente reformular a pergunta.)"
            span.set_attribute("response.length", len(answer))
            return HelpAskResponse(
                answer=answer,
                model=resp.get("model", "azure"),
                usage=resp.get("usage", {}) or {},
            )
        except Exception as e:
            logger.warning(f"help.ask_context falhou: {type(e).__name__}: {e}")
            raise HTTPException(503, f"Assistente IA indisponível: {type(e).__name__}: {str(e)[:160]}")


# ─── V2 endpoint — contexto rico do help-content.js ────────────────────


_PAGE_V2_SYSTEM_PROMPT = (
    "Você é um assistente técnico do Maestro — uma plataforma multi-agente. "
    "O usuário está navegando em uma página específica e tem uma dúvida. "
    "Use o CONTEXTO DA PÁGINA abaixo como verdade canônica.\n\n"
    "O contexto vem estruturado em seções tipadas:\n"
    "- CONCEITO — analogia de alto nível, o que é\n"
    "- FUNDAMENTOS — como funciona por baixo\n"
    "- CAMPOS — cada campo da tela, com explicação + exemplo + default\n"
    "- CASOS DE USO — cenários práticos\n"
    "- EXEMPLO — passo-a-passo concreto\n"
    "- PEGADINHAS — armadilhas comuns (info/warning/danger)\n\n"
    "REGRAS:\n"
    "- Se a pergunta for sobre um campo específico, vá direto em CAMPOS e cite o nome do campo.\n"
    "- Se for sobre 'quando uso isso', combine CONCEITO + CASOS DE USO.\n"
    "- Se for 'como fazer X', combine EXEMPLO + CAMPOS relevantes.\n"
    "- Se for 'cuidado com Y', cite PEGADINHAS.\n"
    "- Se a pergunta sair do escopo da página, redirecione gentilmente.\n"
    "- Tom profissional friendly, em português do Brasil, sem emojis.\n"
    "- Limite ~250 palavras. Use blocos ```bash``` para comandos.\n"
    "- Cite exatamente o nome dos campos/casos quando relevante (ajuda o usuário a encontrar na tela)."
)


def _render_v2_context(req: "HelpAskContextV2Request") -> str:
    """Serializa o contexto V2 em texto plano otimizado para LLM.

    Cada section vira um bloco com cabeçalho claro. Itens (campos, casos,
    pegadinhas) viram sub-blocos com metadados (required, default, severity).
    Strip de HTML em qualquer body porque às vezes o front manda com tags.
    """
    parts: list[str] = [f"=== PÁGINA: {req.title} ==="]
    if req.summary:
        parts.append(f"\nRESUMO: {req.summary}")
    if req.related:
        parts.append(f"PÁGINAS RELACIONADAS: {', '.join(req.related)}")

    for sec in req.sections:
        header = _SECTION_LABEL.get(sec.kind, sec.kind.upper())
        parts.append(f"\n--- {header}: {sec.title} ---")
        if sec.body:
            parts.append(_strip_html(sec.body))
        if sec.items:
            for it in sec.items:
                name = it.name or it.title or ""
                meta_bits: list[str] = []
                if it.required:
                    meta_bits.append("OBRIGATÓRIO")
                if it.default:
                    meta_bits.append(f"default={it.default}")
                if it.severity:
                    meta_bits.append(f"sev={it.severity}")
                if it.options:
                    meta_bits.append(f"opções: {', '.join(it.options)}")
                meta = f" [{' | '.join(meta_bits)}]" if meta_bits else ""
                parts.append(f"• {name}{meta}")
                body_clean = _strip_html(it.body)
                if body_clean:
                    parts.append(f"  {body_clean}")
                if it.example:
                    parts.append(f"  Ex.: {it.example}")
    return "\n".join(parts)


_SECTION_LABEL = {
    "concept":      "CONCEITO",
    "fundamentos":  "FUNDAMENTOS",
    "campos":       "CAMPOS DA TELA",
    "casos_de_uso": "CASOS DE USO",
    "exemplo":      "EXEMPLO PRÁTICO",
    "pegadinhas":   "PEGADINHAS",
}


@router.post("/ask-context-v2", response_model=HelpAskResponse)
async def ask_help_context_v2(req: HelpAskContextV2Request) -> HelpAskResponse:
    """V2 do help contextual — recebe schema rico em vez de plain text.

    Permite respostas muito mais precisas: 'qual o default do campo Temperatura?'
    é respondido lendo direto sections[campos].items[Temperatura].default.

    Frontend usa este endpoint quando `pageHelpV2` está populado (schema novo);
    senão cai no /ask-context legacy.
    """
    with _tracer.start_as_current_span("help.ask_context_v2") as span:
        span.set_attribute("page.title", req.title)
        span.set_attribute("page.sections", len(req.sections))
        span.set_attribute("question.length", len(req.question))

        ctx_text = _render_v2_context(req)

        messages: list[dict] = [
            {"role": "system", "content": _PAGE_V2_SYSTEM_PROMPT},
            {"role": "user", "content": ctx_text + "\n\n=== PERGUNTA DO USUÁRIO ===\n" + req.question},
        ]
        if req.history:
            tail = req.history[-MAX_HISTORY:]
            messages = [messages[0]] + tail + [messages[-1]]

        try:
            provider = get_provider("azure")
            resp = await provider.generate(messages, max_tokens=MAX_TOKENS)
            answer = (resp.get("content") or "").strip()
            if not answer:
                answer = "(O modelo não retornou resposta. Tente reformular a pergunta.)"
            span.set_attribute("response.length", len(answer))
            return HelpAskResponse(
                answer=answer,
                model=resp.get("model", "azure"),
                usage=resp.get("usage", {}) or {},
            )
        except Exception as e:
            logger.warning(f"help.ask_context_v2 falhou: {type(e).__name__}: {e}")
            raise HTTPException(503, f"Assistente IA indisponível: {type(e).__name__}: {str(e)[:160]}")
