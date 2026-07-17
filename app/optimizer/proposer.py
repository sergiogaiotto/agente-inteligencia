"""Propositor GROUNDED de variantes de system_prompt (45.0.0, PR3b).

Estilo MIPROv2 (proposta fundamentada, não no vácuo): o LLM otimizador recebe
(a) o prompt ATUAL em camadas (system_prompt do agente + seções de texto livre
da skill + nota sobre o envelope fixo da plataforma), (b) um RESUMO do gold
set (distribuições; exemplos só de casos normais), (c) as falhas do último run
com os literais de red flag OCULTOS (alimentá-los ensinaria o otimizador a
parafrasear o conteúdo proibido — lição da revisão do PR2), e (d) uma "tip"
de estilo por variante para forçar diversidade.

Guardas anti-vazamento/anti-Goodhart:
- instrução explícita "generalize, não memorize" + DETECTOR de vazamento:
  variante que contém um trecho de gold case é DESCARTADA (com aviso);
- casos ADVERSARIAIS nunca aparecem no contexto (nem input nem gabarito);
- aviso quando a rota LLM do optimizer coincide com a do judge (o mesmo
  modelo propondo e julgando seleciona prompts que agradam a si próprio).

A variante-CONTROLE determinística (derivada do contrato, custo LLM zero) é o
braço de controle barato do comparativo — no paper original do DSPy a
instrução mecânica derivada dos campos já compete bem.
"""
from __future__ import annotations

import json
import re
import logging
from collections import Counter

logger = logging.getLogger(__name__)

# Tips de estilo (MIPROv2 "tips"): 1 por variante, rotacionadas — força
# diversidade entre os K candidatos em vez de K paráfrases do mesmo texto.
STYLE_TIPS: list[tuple[str, str]] = [
    ("concisa",
     "Reescreva de forma CONCISA e direta: corte instruções redundantes; "
     "cada frase deve mudar um comportamento observável."),
    ("passo-a-passo",
     "Estruture o trabalho em passos numerados explícitos (analisar → "
     "verificar evidência → responder), mantendo o contrato de saída."),
    ("persona",
     "Reforce a persona e o tom do agente (voz consistente), sem alterar "
     "nenhuma regra funcional."),
    ("estruturada",
     "Organize as instruções em blocos rotulados (Objetivo, Regras, "
     "Formato da resposta) sem mudar o contrato de saída."),
]

# Trecho mínimo de gold case que caracteriza vazamento se aparecer verbatim
# na variante (janela curta demais = falso positivo com frases comuns).
_LEAK_MIN_CHARS = 24


def strip_red_flag_literals(reasons: list) -> list[str]:
    """Oculta o LITERAL das red flags nos failure_reasons antes de alimentar
    o propositor — o texto proibido não pode virar contexto de reescrita
    (o otimizador aprenderia a parafraseá-lo sem a substring)."""
    out = []
    for r in reasons or []:
        out.append(re.sub(r"red_flag=.*", "red_flag=[conteúdo proibido oculto]",
                          str(r)))
    return out


def _norm_ws(text: str) -> str:
    """Normaliza p/ comparação de vazamento: lowercase + whitespace colapsado
    (LLMs re-quebram linhas; sem isso o detector era derrotável por reformat)."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def variant_leaks_gold(variant_text: str, gold_cases: list[dict],
                       *, allow_fragments: tuple | list = ()) -> bool:
    """Detector de vazamento (guard anti-overfit do plano): a variante contém
    trecho verbatim de gold case (input OU gabarito)?

    Varredura por JANELAS DESLIZANTES (review PR3b: as duas janelas ancoradas
    originais deixavam passar cópias do MEIO/FIM de casos longos): janelas de
    40 chars com passo 20 cobrem qualquer cópia ≥60 chars e cópias ≥40
    alinhadas; fragmentos curtos (24–40) são checados inteiros.

    `allow_fragments`: textos que o PRÓPRIO contexto enviou ao LLM (os
    exemplos_de_entrada) — ecoá-los é ilustração legítima, não memorização;
    sem esta lista o detector rejeitava variantes por eco do material que nós
    mesmos fornecemos (contradição autoimposta apontada na review)."""
    low = _norm_ws(variant_text)
    if not low:
        return False
    allowed = {_norm_ws(a) for a in (allow_fragments or ()) if a}
    win, step = 40, 20
    for c in gold_cases or []:
        for field in ("input_text", "expected_output"):
            frag = _norm_ws(c.get(field))
            if len(frag) < _LEAK_MIN_CHARS or frag in allowed:
                continue
            if len(frag) <= win:
                if frag in low:
                    return True
                continue
            for i in range(0, len(frag) - win + 1, step):
                if frag[i:i + win] in low:
                    return True
            # cauda (o range pode não alcançar o fim exato)
            if frag[-win:] in low:
                return True
    return False


def build_control_variant(agent: dict, skill_sections: dict | None) -> dict:
    """Variante-CONTROLE determinística: instrução mecânica derivada do
    contrato (## Inputs + Output Contract), zero LLM. Braço de controle do
    comparativo — se o LLM propositor não vence nem isto, a paisagem é plana."""
    name = (agent.get("name") or "o agente").strip()
    parts = [f"Você é {name}."]
    sections = skill_sections or {}
    purpose = (sections.get("purpose") or "").strip()
    if purpose:
        first = purpose.splitlines()[0].strip()
        if first:
            parts.append(f"Objetivo: {first}")
    inputs = (sections.get("inputs") or "").strip()
    if inputs:
        # Campos de TOPO do ## Inputs via SSOT (review PR3b: a regex crua
        # achatava propriedades aninhadas como se fossem entradas de topo —
        # exatamente a classe de cópia que inputs_schema.py existe p/ matar).
        fields: list[str] = []
        try:
            from app.skill_parser.inputs_schema import extract_inputs_schema
            # o SSOT localiza a seção pelo heading — skill_sections guarda só
            # o corpo, então re-prefixa "## Inputs" antes de extrair.
            schema = extract_inputs_schema("## Inputs\n" + inputs) or {}
            fields = list((schema.get("properties") or {}).keys())[:8]
        except Exception:
            fields = []
        if fields:
            parts.append("Entradas disponíveis: " + ", ".join(fields) + ".")
    contract = (sections.get("output_contract") or "").strip()
    if contract:
        parts.append("Siga ESTRITAMENTE o contrato de saída da skill.")
    parts.append("Responda com base apenas nas evidências disponíveis; "
                 "quando faltar informação, diga o que falta.")
    return {
        "kind": "control",
        "style_tip": "determinística",
        "system_prompt": " ".join(parts),
        "rationale": "Variante-controle derivada mecanicamente do contrato "
                     "(sem LLM). Se nenhuma proposta vencer nem o controle, "
                     "a paisagem de otimização é plana para esta skill.",
    }


def summarize_gold(cases: list[dict]) -> dict:
    """Resumo do gold set para o contexto grounded. Casos ADVERSARIAIS entram
    só na CONTAGEM — nem input nem gabarito deles vão ao propositor."""
    normal = [c for c in cases if (c.get("case_type") or "normal") != "adversarial"]
    adversarial_n = len(cases) - len(normal)
    cats = Counter((c.get("category") or "(sem categoria)") for c in normal)
    states = Counter((c.get("expected_state") or "?") for c in normal)
    samples = [
        (c.get("input_text") or "")[:160]
        for c in normal[:3] if (c.get("input_text") or "").strip()
    ]
    return {
        "total": len(cases),
        "normal": len(normal),
        "adversarial": adversarial_n,
        "categorias": dict(cats),
        "estados_esperados": dict(states),
        "exemplos_de_entrada": samples,
    }


def summarize_last_run(run: dict | None) -> dict | None:
    """Falhas do último run concluído (qualquer tipo, com o tipo anotado) —
    o 'feedback' grounded do propositor. Red flags STRIPPED na fonte."""
    if not run:
        return None
    details = run.get("details") or []
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except (json.JSONDecodeError, TypeError):
            details = []
    failures = []
    for d in details:
        if not isinstance(d, dict) or d.get("passed"):
            continue
        reasons = strip_red_flag_literals(d.get("failure_reasons") or [])
        if d.get("error"):
            reasons.append("erro de execução (detalhes omitidos)")
        failures.append({
            "categoria": d.get("category"),
            "esperado": d.get("expected_state"),
            "obtido": d.get("actual_state"),
            "motivos": reasons[:3],
        })
        if len(failures) >= 12:
            break
    return {
        "run_type": run.get("run_type"),
        "accuracy": run.get("accuracy"),
        "gate_result": run.get("gate_result"),
        "falhas": failures,
    }


def build_proposer_messages(*, agent: dict, skill_sections: dict | None,
                            gold_summary: dict, last_run: dict | None,
                            style_key: str, style_tip: str) -> list[dict]:
    """Mensagens do LLM otimizador — proposta GROUNDED, edição minimal."""
    sections = skill_sections or {}

    def _clip(text: str, n: int) -> str:
        # Corte COM marcador (review PR3b: cortes silenciosos deixavam o
        # modelo raciocinar sobre um workflow pela metade sem saber).
        t = (text or "").strip()
        return t if len(t) <= n else t[:n] + "\n[[…cortado para o contexto]]"

    system = (
        "Você é um OTIMIZADOR de system prompts de agentes LLM de uma "
        "plataforma de atendimento. Sua tarefa: propor UMA variante do "
        "system_prompt do agente que melhore a taxa de acerto no conjunto "
        "de avaliação descrito.\n"
        "REGRAS INEGOCIÁVEIS:\n"
        "1. Reescreva SOMENTE o system_prompt (texto livre). As seções da "
        "skill (## Decisions, ## Inputs, contratos) são SELADAS e o envelope "
        "da plataforma (diretivas de idioma, grounding e decisão, que abrem "
        "E fecham o prompt final) VENCE qualquer instrução sua — não tente "
        "contrariá-los.\n"
        "2. GENERALIZE, NÃO MEMORIZE: é PROIBIDO copiar textos dos casos de "
        "avaliação para o prompt (vazamento — a variante será rejeitada).\n"
        "3. Edição MINIMAL: preserve a voz e a intenção do prompt atual; "
        "mude o que as falhas indicam, não tudo.\n"
        "4. Responda em pt-BR, APENAS com JSON válido no formato "
        '{"system_prompt": "...", "rationale": "..."} — rationale = 2-3 '
        "frases explicando O QUE mudou e POR QUÊ (vira o registro de "
        "auditoria da variante).\n"
        f"DICA DE ESTILO desta variante ({style_key}): {style_tip}"
    )
    ctx = {
        # Cap também no prompt atual (review PR3b: ilimitado, um prompt de
        # 20k chars — ex.: variante adotada num ciclo anterior — inflava as
        # K chamadas; e a assimetria punia justamente a peça central).
        "system_prompt_atual": _clip(agent.get("system_prompt") or "", 4000)
        or "(vazio — o default da plataforma é 'Você é um agente inteligente.')",
        "skill_texto_livre": {
            "purpose": _clip(sections.get("purpose") or "", 1500),
            "workflow": _clip(sections.get("workflow") or "", 1500),
            "output_contract": _clip(sections.get("output_contract") or "", 800),
            "guardrails": _clip(sections.get("guardrails") or "", 800),
        },
        "aviso_envelope": (
            "O prompt FINAL executado é montado em camadas: diretiva de "
            "idioma → grounding → tamanho → SEU system_prompt → seções da "
            "skill → diretiva selada de decisão → catálogo de tools → "
            "fechamentos de grounding e idioma."
        ),
        "resumo_do_gold_set": gold_summary,
        "ultimo_run": last_run or "(nenhum run concluído deste alvo ainda)",
    }
    user = (
        "CONTEXTO GROUNDED DO ALVO:\n"
        + json.dumps(ctx, ensure_ascii=False, indent=2)
        + "\n\nProponha a variante agora (APENAS o JSON)."
    )
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


def parse_proposer_response(content: str) -> dict | None:
    """Extrai {'system_prompt','rationale'} da resposta do LLM via o parser
    CANÔNICO extract_args_json (review PR3b: a cópia local nº 6 de "JSON
    robusto" usava strip_code_fence — que não conhece ```json — e um regex
    ganancioso {.*} que abraçava prosa; o canônico tolera cercas rotuladas,
    prosa em volta e faz varredura balanceada). None = imprestável."""
    from app.agents.args_suggest import extract_args_json
    obj, _err = extract_args_json(content or "")
    if not isinstance(obj, dict):
        return None
    sp = (obj.get("system_prompt") or "").strip()
    if not sp:
        return None
    return {"system_prompt": sp[:20000],
            "rationale": (obj.get("rationale") or "").strip()[:2000]}
