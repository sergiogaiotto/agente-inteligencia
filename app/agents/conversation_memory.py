"""Memória de conversa multi-turno (2026-06-06).

Por que existe: até aqui o pipeline era *stateless por turno* — o seed do grafo
(`engine`: `state["messages"]`) levava só o turno ATUAL. A sessão era persistida
(`turns`: `user_text_redacted` / `output_text_redacted`, em LINHAS SEPARADAS — o
FSM grava o input no Intake e o output no LogAndClose, com turn_number+1), e o
workspace já reconstruía a conversa pra UI, mas esse histórico NUNCA voltava ao
LLM nem ao gate de roteamento. Resultado real (bug "Doc Analise", turno 2):
follow-ups como "sobre qual o tema" chegavam sem contexto → o roteador respondia
"não consegui entender sua pergunta".

Este módulo é a FONTE ÚNICA de reconstrução de turnos (anti-drift com a UI de
`workspace.get_session`):
- `load_conversation_turns()` — normaliza as linhas de `turns` em mensagens
  cronológicas `[{role, content, turn_number}]`, decodificando o JSON legado de
  recusa/escalação igual o workspace faz (mesma decodificação → sem drift).
- `build_history_messages()` — adapta pra LangChain (`HumanMessage`/`AIMessage`)
  já com a JANELA por camada e teto de chars.
- `session_text_window()` — junta os textos do USUÁRIO recentes; vira o sinal
  pegajoso do gate condicional (`text_all` passa a casar follow-ups por keyword).

Escopo por camada (decisão do operador 2026-06-06): o ROTEADOR é onde o
follow-up mais importa (resolve anáfora "sobre qual o tema") → janela MÉDIA; o
orquestrador coordena, contexto mais enxuto → janela LEVE; o subagente é tarefa-
folha focada e já recebe `pipeline_context` do upstream → OFF por padrão
(opt-in futuro).

`context_mode` (parâmetro de API, default `'auto'`):
- `'none'`    → stateless puro (comportamento legado). Função pura p/ integrações
                idempotentes / sensíveis a privacidade.
- `'auto'`    → servidor reconstrói a janela por `session_id`, escopada por camada.
- `'client'`  → bring-your-own (o chamador traz o histórico). RESERVADO: hoje cai
                em `'auto'` (reconstrução server-side) até o contrato `history`
                ser aceito ponta-a-ponta.
- `'summary'` → resumo rolante. RESERVADO: hoje cai em `'auto'`.

Toda falha é fail-open silencioso: histórico é melhoria, nunca pode derrubar a
execução (retorna `[]` / `""`). Os textos vêm de `*_redacted` — já passaram pelo
DLP na persistência.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger("app.agents.conversation_memory")

# Janela por camada, em nº de mensagens (cada linha de `turns` = 1 mensagem:
# input do usuário OU output do agente). router=médio, aobd=leve, subagent=off.
HISTORY_WINDOW_BY_KIND: dict[str, int] = {
    "router": 8,     # AR — médio: resolve follow-up/anáfora ("sobre qual o tema")
    "aobd": 4,       # AOBD — leve: coordena, contexto enxuto
    "subagent": 0,   # SA — off: tarefa-folha focada (já tem pipeline_context)
}
DEFAULT_HISTORY_WINDOW = 4  # kind desconhecido → leve

# Teto de caracteres do histórico injetado (corta do mais ANTIGO, mantém o
# recente). Evita estourar tokens em conversas longas.
HISTORY_CHAR_BUDGET = 4000

# Sinais pegajosos do gate: janela (em mensagens de USUÁRIO) e teto de chars.
SESSION_TEXT_WINDOW_TURNS = 6
SESSION_TEXT_CHAR_BUDGET = 1500

VALID_CONTEXT_MODES: tuple[str, ...] = ("none", "auto", "client", "summary")


def normalize_context_mode(mode: str | None) -> str:
    """Saneia o parâmetro vindo da API. Default seguro = 'auto'."""
    m = (mode or "").strip().lower()
    return m if m in VALID_CONTEXT_MODES else "auto"


def context_enabled(mode: str | None) -> bool:
    """True se o modo dispara reconstrução de contexto (tudo menos 'none')."""
    return normalize_context_mode(mode) != "none"


def history_window_for_kind(kind: str | None) -> int:
    """Tamanho da janela (mensagens) p/ a camada do agente."""
    return HISTORY_WINDOW_BY_KIND.get(
        (kind or "").strip().lower(), DEFAULT_HISTORY_WINDOW
    )


def _decode_legacy_content(content: str) -> str:
    """Decodifica o JSON legado de recusa/escalação em texto legível.

    ESPELHA `workspace.get_session` (linhas ~206-214) — mesma conversão pra que
    o LLM veja a conversa EXATAMENTE como a UI mostra (anti-drift).
    """
    if content and content.startswith("{") and '"type"' in content:
        try:
            p = json.loads(content)
            if p.get("type") == "refusal":
                return (
                    f"⚠ Recusa controlada: {p.get('reason','')}\n\n"
                    f"Próximo passo: {p.get('next_step','')}"
                )
            if p.get("type") == "escalation":
                return f"🔺 Escalação: {p.get('reason','')}"
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return content


async def load_conversation_turns(
    session_id: str,
    *,
    before_turn: int | None = None,
    limit: int = 200,
) -> list[dict]:
    """Reconstrói a conversa de uma sessão como mensagens cronológicas.

    Retorna `[{"role": "user"|"assistant", "content": str, "turn_number": int}]`
    ordenado por `turn_number` asc. `before_turn`, se informado, EXCLUI turnos
    com `turn_number >= before_turn` — usado pra não duplicar o turno atual, que
    o FSM já persistiu (no Intake) ANTES do grafo rodar.

    Fail-open: qualquer erro de DB → `[]` (loga warning).
    """
    if not session_id:
        return []
    from app.core.database import turns_repo

    try:
        rows = await turns_repo.find_all(interaction_id=session_id, limit=limit)
    except Exception as e:  # DB down, etc — histórico é opcional
        logger.warning(
            "conversation_memory.load_failed",
            extra={
                "event": "context.load",
                "session_id": session_id,
                "error_type": type(e).__name__,
                "error_msg": str(e)[:200],
            },
        )
        return []

    msgs: list[dict] = []
    for t in rows:
        tn = int(t.get("turn_number") or 0)
        if before_turn is not None and tn >= before_turn:
            continue
        u = (t.get("user_text_redacted") or "").strip()
        o = (t.get("output_text_redacted") or "").strip()
        if u:
            msgs.append({"role": "user", "content": u, "turn_number": tn})
        if o:
            msgs.append(
                {"role": "assistant", "content": _decode_legacy_content(o), "turn_number": tn}
            )
    msgs.sort(key=lambda m: m["turn_number"])
    return msgs


def _apply_char_budget(msgs: list[dict], budget: int) -> list[dict]:
    """Corta do MAIS ANTIGO até caber em `budget` chars (mantém os recentes)."""
    if budget <= 0 or not msgs:
        return msgs
    total = 0
    kept_rev: list[dict] = []
    for m in reversed(msgs):
        c = len(m.get("content", "") or "")
        if total + c > budget and kept_rev:
            break
        total += c
        kept_rev.append(m)
    kept_rev.reverse()
    return kept_rev


async def build_history_messages(
    session_id: str,
    kind: str | None,
    context_mode: str | None,
    *,
    before_turn: int | None = None,
) -> list:
    """Mensagens LangChain (`HumanMessage`/`AIMessage`) do histórico recente,
    prontas pra prefixar o seed do grafo.

    Retorna `[]` quando: `context_mode == 'none'`, sem `session_id`, janela da
    camada `== 0` (ex.: subagent), ou sem turnos anteriores. `'client'`/
    `'summary'` ainda se comportam como `'auto'` (reconstrução server-side).
    """
    if not context_enabled(context_mode) or not session_id:
        return []
    window = history_window_for_kind(kind)
    if window <= 0:
        return []
    turns = await load_conversation_turns(session_id, before_turn=before_turn)
    if not turns:
        return []
    turns = turns[-window:]
    turns = _apply_char_budget(turns, HISTORY_CHAR_BUDGET)

    from langchain_core.messages import AIMessage, HumanMessage

    out: list = []
    for m in turns:
        if m["role"] == "user":
            out.append(HumanMessage(content=m["content"]))
        else:
            out.append(AIMessage(content=m["content"]))
    return out


async def session_text_window(
    session_id: str,
    context_mode: str | None,
    *,
    before_turn: int | None = None,
) -> str:
    """Texto das PERGUNTAS recentes do usuário (lowercase) — o sinal pegajoso
    que o gate condicional mistura em `text_all` pra casar follow-ups.

    Só o texto do USUÁRIO (não os outputs do agente) — assim o roteador não
    perpetua um ramo pelo próprio texto. `''` quando context off / sem sessão /
    sem histórico. Capado por `SESSION_TEXT_CHAR_BUDGET` (mantém o mais recente).
    """
    if not context_enabled(context_mode) or not session_id:
        return ""
    turns = await load_conversation_turns(session_id, before_turn=before_turn)
    users = [m["content"] for m in turns if m["role"] == "user"]
    if not users:
        return ""
    users = users[-SESSION_TEXT_WINDOW_TURNS:]
    text = " ".join(users).lower().strip()
    if len(text) > SESSION_TEXT_CHAR_BUDGET:
        text = text[-SESSION_TEXT_CHAR_BUDGET:]
    return text
