"""Parser canônico de SKILL.md — §5 da especificação.

Extrai frontmatter YAML e seções obrigatórias/opcionais do Markdown,
valida estrutura e gera hash de conteúdo.

MELHORIA 2026-04-21: Seção ## Execution Profile com auto-inferência.
Determina modo de execução (fast/standard/rigorous) para otimizar
pipeline — elimina chamadas LLM desnecessárias.
"""

import logging
import re
import hashlib
import json
import yaml
from dataclasses import dataclass, field, asdict
from typing import Optional


logger = logging.getLogger(__name__)


REQUIRED_SECTIONS = ["Purpose", "Activation Criteria", "Inputs", "Workflow", "Tool Bindings", "Output Contract", "Failure Modes"]
OPTIONAL_SECTIONS = ["Delegations", "Compensation", "Guardrails", "Budget", "Examples", "Telemetry", "Data Dependencies", "Model Constraints", "Evidence Policy", "Gold Refs", "Execution Profile", "API Bindings", "Data Tables", "Output Shape", "Response Template"]
VALID_KINDS = {"orchestrator", "router", "subagent"}
VALID_STABILITY = {"alpha", "beta", "stable", "deprecated"}
VALID_EXEC_MODES = {"fast", "standard", "rigorous", "declarative"}


@dataclass
class SkillFrontmatter:
    id: str = ""
    version: str = "0.1.0"
    kind: str = "subagent"
    owner: str = ""
    stability: str = "alpha"
    execution_mode: str = ""


@dataclass
class ParsedSkill:
    frontmatter: SkillFrontmatter = field(default_factory=SkillFrontmatter)
    name: str = ""
    purpose: str = ""
    activation_criteria: str = ""
    inputs: str = ""
    workflow: str = ""
    tool_bindings: str = ""
    output_contract: str = ""
    failure_modes: str = ""
    delegations: str = ""
    compensation: str = ""
    guardrails: str = ""
    budget: str = ""
    examples: str = ""
    telemetry: str = ""
    data_dependencies: str = ""
    model_constraints: str = ""
    evidence_policy: str = ""
    gold_refs: str = ""
    execution_profile: str = ""
    execution_mode: str = ""       # fast | standard | rigorous | declarative
    api_bindings: str = ""         # raw text (markdown or YAML)
    api_bindings_parsed: list = field(default_factory=list)  # list of dicts
    # Onda 6 Wave 2: evidence_policy parseado (governance skill ↔ source).
    # Quando há bloco fenced YAML em ## Evidence Policy: dict com keys
    # opcionais {sources, min_relevance, max_age_days, cite_sources, raw}.
    # Sem fence ou parse-fail: {raw: <texto cru>} — comportamento legacy.
    evidence_policy_parsed: dict = field(default_factory=dict)
    # Onda Tabular: ## Data Tables permite skill consultar tabelas DuckDB
    # promovidas a partir de CSV/XLSX em Bases de Conhecimento. Bloco YAML
    # com lista de tables[]: {id, table_ref (urn), inputs[], query{select,
    # filters[], order_by, limit}, output_mapping?, on_error?}. Engine
    # executa query parametrizada (sem LLM gerando SQL) — paridade com
    # ## API Bindings em termos de modelo declarativo.
    data_tables: str = ""          # raw text (markdown or YAML)
    data_tables_parsed: list = field(default_factory=list)  # list of dicts
    # Onda 1 Output Shape: presets de tamanho/forma da resposta. Bloco YAML
    # opcional em `## Output Shape`. Parser extrai `length_preset` (validado
    # contra LENGTH_PRESETS de output_shape.py). Default no engine quando
    # ausente: 'digest' (1500 chars).
    output_shape: str = ""         # raw text
    output_shape_parsed: dict = field(default_factory=dict)  # {length_preset, max_chars}
    # Frase humana DETERMINÍSTICA (sem LLM): corpo Jinja2 CRU do ## Response
    # Template, renderizado pelo declarative_engine contra {inputs, context} ao
    # final da execução. Vazio → engine devolve o retorno estruturado legado
    # (compat). NÃO satisfaz o gate declarativo (não é fonte de dados).
    response_template: str = ""    # raw text (corpo Jinja2, fence opcional)
    raw_content: str = ""
    content_hash: str = ""
    validation_errors: list = field(default_factory=list)
    is_valid: bool = True


def parse_skill_md(content: str) -> ParsedSkill:
    """Faz parsing completo de um SKILL.md conforme anatomia canônica §5.
    Resiliente: remove code fences, busca frontmatter em qualquer posição,
    gera defaults quando ausente."""
    # ── Pré-processamento: remove code fences do wizard ──
    cleaned = content.strip()
    cleaned = re.sub(r'^```(?:markdown|md|yaml)?\s*\n', '', cleaned)
    cleaned = re.sub(r'\n```\s*$', '', cleaned)
    cleaned = cleaned.strip()

    result = ParsedSkill(raw_content=content)
    result.content_hash = hashlib.sha256(content.encode()).hexdigest()

    # ── Frontmatter YAML — busca em qualquer posição ──
    fm_match = re.search(r"^---\s*\n(.*?)\n---", cleaned, re.DOTALL | re.MULTILINE)
    if fm_match:
        try:
            fm_data = yaml.safe_load(fm_match.group(1)) or {}
            result.frontmatter = SkillFrontmatter(
                id=fm_data.get("id", ""),
                version=str(fm_data.get("version", "0.1.0")),
                kind=fm_data.get("kind", "subagent"),
                owner=fm_data.get("owner", ""),
                stability=fm_data.get("stability", "alpha"),
                execution_mode=str(fm_data.get("execution_mode", "")).strip().lower(),
            )
            if result.frontmatter.execution_mode and result.frontmatter.execution_mode not in VALID_EXEC_MODES:
                result.validation_errors.append(
                    f"execution_mode inválido: {result.frontmatter.execution_mode}. Válidos: {VALID_EXEC_MODES}"
                )
                result.frontmatter.execution_mode = ""
            if result.frontmatter.kind not in VALID_KINDS:
                result.validation_errors.append(f"kind inválido: {result.frontmatter.kind}. Válidos: {VALID_KINDS}")
            if result.frontmatter.stability not in VALID_STABILITY:
                result.validation_errors.append(f"stability inválido: {result.frontmatter.stability}")
        except yaml.YAMLError as e:
            result.validation_errors.append(f"Frontmatter YAML inválido: {e}")
    else:
        result.validation_errors.append("Frontmatter YAML ausente — usando defaults (alpha/subagent)")
        result.frontmatter = SkillFrontmatter()

    # ── Nome (H1) — busca no conteúdo limpo ──
    h1_match = re.search(r"^#\s+(.+)$", cleaned, re.MULTILINE)
    if h1_match:
        result.name = h1_match.group(1).strip()
    else:
        for line in cleaned.split("\n"):
            line = line.strip()
            if line and not line.startswith("---") and not line.startswith("```"):
                result.name = line[:100]
                break
        if not result.name:
            result.name = "Skill sem nome"
        result.validation_errors.append("Nome (heading H1 com #) ausente — extraído da primeira linha")

    # ── Seções (H2) ──
    sections = _extract_sections(cleaned)

    for section_name in REQUIRED_SECTIONS:
        key = _section_to_attr(section_name)
        value = sections.get(section_name, "")
        setattr(result, key, value)
        if not value.strip():
            result.validation_errors.append(f"Seção obrigatória ausente: ## {section_name}")

    for section_name in OPTIONAL_SECTIONS:
        key = _section_to_attr(section_name)
        value = sections.get(section_name, "")
        setattr(result, key, value)

    # ── Execution Mode — precedência: frontmatter > Execution Profile > inferência ──
    if result.frontmatter.execution_mode:
        result.execution_mode = result.frontmatter.execution_mode
    elif result.execution_profile.strip():
        result.execution_mode = _parse_execution_mode(result.execution_profile)
    else:
        result.execution_mode = _infer_execution_mode(result)

    # ── API Bindings — parse YAML para lista de dicts ──
    if result.api_bindings.strip():
        result.api_bindings_parsed = _parse_api_bindings(result.api_bindings)
    # Validação declarativa adiada: pode ser satisfeita por ## API Bindings
    # OU ## Data Tables (ver bloco abaixo). Aguarda parse de data_tables
    # antes de decidir.

    # ── Evidence Policy — parse YAML estruturado (Onda 6 Wave 2) ──
    # Quando há bloco fenced YAML em ## Evidence Policy, extrai sources/limits/
    # flags. Sem fence: continua como texto cru (sem governance ativa = legacy).
    result.evidence_policy_parsed = _parse_evidence_policy(result.evidence_policy)

    # ── Data Tables — parse YAML estruturado (Onda Tabular) ──
    # Espelha pattern de api_bindings_parsed: aceita fence ```yaml ... ``` ou
    # YAML inline começando com `tables:` ou `- id:`. Validação estrutural
    # mínima — engine valida coluna/operador contra schema real da tabela.
    if result.data_tables.strip():
        result.data_tables_parsed = _parse_data_tables(result.data_tables)

    # ── Output Shape — preset de tamanho da resposta (Onda 1 do roadmap) ──
    # Bloco YAML opcional: `length_preset: digest`. Validado contra
    # LENGTH_PRESETS. Preset inválido → erro de validação (não silencioso —
    # operador precisa saber que digitou typo). Skill sem o bloco vira
    # comportamento default no engine ("digest" — 1500 chars).
    if result.output_shape.strip():
        result.output_shape_parsed = _parse_output_shape(result.output_shape)
        invalid_preset = result.output_shape_parsed.get("_invalid_preset")
        if invalid_preset:
            result.validation_errors.append(
                f"length_preset inválido em ## Output Shape: '{invalid_preset}'. "
                f"Válidos: intent, summary, digest, analysis, report, unbounded."
            )

    # Validação declarative: pelo menos UMA fonte declarativa precisa existir
    # (API Bindings OU Data Tables). Skills puras MCP/RAG não usam declarative.
    if result.execution_mode == "declarative" and not (
        result.api_bindings_parsed or result.data_tables_parsed
    ):
        result.validation_errors.append(
            "execution_mode=declarative exige ## API Bindings OU ## Data Tables "
            "com pelo menos 1 entrada válida"
        )

    result.is_valid = len(result.validation_errors) == 0
    return result


def _extract_fenced_yaml_body(section_text: str) -> str:
    """Extrai o corpo de um bloco ```yaml ... ``` dentro de section_text.

    Devolve o conteúdo entre o fence de abertura e o PRIMEIRO fence de
    fechamento — independente de haver texto, horizontal rule (`---`)
    ou outra seção depois. Quando não há fence, devolve `section_text`
    inteiro (modo inline).

    Histórico (bug fixado em 2026-06-01): o parser usava
    `re.sub(r"\\n```\\s*$", "", body)`, que só removia o fence quando
    ele estava no FINAL ABSOLUTO da string. Como `_extract_sections`
    inclui tudo até o próximo `## `, sempre havia conteúdo após o
    fence de fechamento (no mínimo o HR `\\n\\n---\\n\\n` que o wizard
    injeta entre seções obrigatórias). Resultado: o ``` literal sobrava
    no body, `yaml.safe_load` levantava `ScannerError` ao encontrar a
    crase, o `except yaml.YAMLError: return []` engolia em silêncio e
    a validação reportava "execution_mode=declarative exige ## API
    Bindings ... com pelo menos 1 entrada válida" mesmo com o binding
    visualmente presente no SKILL.md.
    """
    fence_open = re.search(r"```(?:yaml|yml)?\s*\n", section_text)
    if not fence_open:
        return section_text
    rest = section_text[fence_open.end():]
    fence_close = rest.find("\n```")
    if fence_close == -1:
        return rest
    return rest[:fence_close]


def _parse_api_bindings(section_text: str) -> list[dict]:
    """Parse a seção ## API Bindings como lista YAML.

    Aceita:
      1. Bloco fencado ```yaml ... ``` (fechamento opcional — o
         pré-processador da SKILL remove trailing ``` global).
      2. Conteúdo inline (sem fence) começando com '- id:'.
      3. Mapping no topo com chave 'endpoints:' OU 'bindings:' contendo
         a lista (formato emitido pelo wizard "IA, me ajude" — 2026-05-31).
         Antes desta tolerância, SKILL.md gerado pelo wizard caía no
         `return []` e disparava "execution_mode=declarative exige ##
         API Bindings com pelo menos 1 entrada válida" mesmo tendo o
         binding lá dentro.

    Retorna lista de dicts; em erro retorna lista vazia silenciosamente
    (validação estrutural fica na camada do engine).
    """
    if not section_text:
        return []

    body = _extract_fenced_yaml_body(section_text)

    try:
        data = yaml.safe_load(body)
    except yaml.YAMLError as e:
        # Antes silencioso (return []). Bug #244 mostrou que YAML
        # quebrado em ## API Bindings derrubava o binding sem deixar
        # rastro — operador via erro "exige ## API Bindings ..." mas
        # não sabia que o YAML estava mal formatado. Agora vai pro
        # errors.log com preview pra debug imediato.
        logger.warning(
            "parser.api_bindings_yaml_invalid",
            extra={
                "event": "skill_parser.yaml_invalid",
                "section": "API Bindings",
                "body_preview": body[:200],
                "error_type": type(e).__name__,
                "error_msg": str(e)[:200],
            },
        )
        return []

    # Tolerância: aceita mapping com chave 'endpoints' ou 'bindings'
    # (formato emitido pelo wizard via prompt — variação comum de LLM).
    if isinstance(data, dict):
        for wrapper in ("endpoints", "bindings"):
            inner = data.get(wrapper)
            if isinstance(inner, list):
                data = inner
                break

    if isinstance(data, list):
        return [_normalize_yaml11_bool_keys(b) for b in data if isinstance(b, dict) and b.get("id")]
    return []


def _parse_data_tables(section_text: str) -> list[dict]:
    """Parse a seção ## Data Tables como lista YAML (Onda Tabular).

    Aceita 3 formas equivalentes:
      1. Fence YAML com chave `tables:` no topo:
         ```yaml
         tables:
           - id: vendas_q4
             table_ref: urn:table:abcd1234:vendas-q4:1
             ...
         ```
      2. Fence YAML com lista direta (sem `tables:`):
         ```yaml
         - id: vendas_q4
           table_ref: urn:table:abcd1234:vendas-q4:1
           ...
         ```
      3. Conteúdo inline sem fence começando com `tables:` ou `- id:`.

    Cada item DEVE ter `id` (único na skill) e `table_ref` (URN da data_table).
    Itens sem esses campos são descartados silenciosamente — validação dura
    fica no engine para mostrar erro contextual ao executar.

    Retorna lista (eventualmente vazia) — nunca levanta exceção.
    """
    if not section_text:
        return []

    body = _extract_fenced_yaml_body(section_text)

    try:
        data = yaml.safe_load(body)
    except yaml.YAMLError as e:
        logger.warning(
            "parser.data_tables_yaml_invalid",
            extra={
                "event": "skill_parser.yaml_invalid",
                "section": "Data Tables",
                "body_preview": body[:200],
                "error_type": type(e).__name__,
                "error_msg": str(e)[:200],
            },
        )
        return []

    # Aceita dict {tables: [...]} OU list direta
    if isinstance(data, dict) and isinstance(data.get("tables"), list):
        items = data["tables"]
    elif isinstance(data, list):
        items = data
    else:
        return []

    parsed = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if not item.get("id") or not item.get("table_ref"):
            continue
        parsed.append(_normalize_yaml11_bool_keys(item))
    return parsed


def _parse_output_shape(section_text: str) -> dict:
    """Parse a seção ## Output Shape (Onda 1 do roadmap de Output Control).

    Aceita bloco fenced YAML:
        ```yaml
        length_preset: digest   # intent|summary|digest|analysis|report|unbounded
        ```

    Sem fence: tenta parsear como YAML inline (linha única `length_preset: X`).
    Sem YAML válido: devolve {} (skill cai no default do engine).

    Returns:
        dict com `length_preset` validado E `max_chars` resolvido. Preset
        inválido entra como `_invalid_preset` pro caller adicionar erro de
        validação — não silencioso.
    """
    if not section_text or not section_text.strip():
        return {}

    fence_open = re.search(r"```(?:yaml|yml)?\s*\n", section_text)
    if fence_open:
        body = section_text[fence_open.end():]
        body = re.sub(r"\n```\s*$", "", body)
    else:
        body = section_text

    try:
        data = yaml.safe_load(body)
    except yaml.YAMLError as e:
        logger.warning(
            "parser.output_shape_yaml_invalid",
            extra={
                "event": "skill_parser.yaml_invalid",
                "section": "Output Shape",
                "body_preview": body[:200],
                "error_type": type(e).__name__,
                "error_msg": str(e)[:200],
            },
        )
        return {}

    if not isinstance(data, dict):
        return {}

    result: dict = {"raw": section_text}
    raw_preset = data.get("length_preset")
    if raw_preset:
        from app.skill_parser.output_shape import is_valid_preset, get_max_chars
        preset_str = str(raw_preset).strip().lower()
        if is_valid_preset(preset_str):
            result["length_preset"] = preset_str
            result["max_chars"] = get_max_chars(preset_str)
        else:
            # Sinaliza pro caller — não absorve silenciosamente. Operador
            # provavelmente digitou typo (ex: "digist" em vez de "digest").
            result["_invalid_preset"] = preset_str
    return result


def _parse_evidence_policy(section_text: str) -> dict:
    """Parse a seção ## Evidence Policy (Onda 6 Wave 2 — governance skill↔source).

    Aceita bloco fenced YAML com schema opcional:
        ```yaml
        sources:
          - <knowledge_source_id>   # opcional comentário humano
        min_relevance: 0.3          # threshold rejeita chunks com score < N
        max_age_days: 90            # source com last_updated > N → ignora
        cite_sources: true          # Wave 3 — força LLM a citar [E1] na resposta
        ```

    Sem bloco fence: devolve {raw: <texto cru>} — legacy mode (sem filtro
    aplicado pelo retriever, mantém comportamento histórico).

    Distinção crítica:
    - chave `sources` ausente → `result["sources"]` ausente → retriever não filtra.
    - chave `sources: []` → `result["sources"] = []` → retriever bloqueia tudo.

    Retorno: dict sempre. Em qualquer caminho de erro devolve `{raw: ...}` —
    nunca levanta exceção.
    """
    if not section_text or not section_text.strip():
        return {}

    # Procura bloco fenced (yaml/yml). Sem fence → legacy mode.
    fence_open = re.search(r"```(?:yaml|yml)?\s*\n", section_text)
    if not fence_open:
        return {"raw": section_text}

    body = section_text[fence_open.end():]
    body = re.sub(r"\n```\s*$", "", body)

    try:
        data = yaml.safe_load(body)
    except yaml.YAMLError as e:
        logger.warning(
            "parser.evidence_policy_yaml_invalid",
            extra={
                "event": "skill_parser.yaml_invalid",
                "section": "Evidence Policy",
                "body_preview": body[:200],
                "error_type": type(e).__name__,
                "error_msg": str(e)[:200],
            },
        )
        return {"raw": section_text}

    if not isinstance(data, dict):
        return {"raw": section_text}

    result: dict = {"raw": section_text}

    # sources: lista explícita (vazia ok = blocked, ausente = sem filtro).
    sources = data.get("sources")
    if isinstance(sources, list):
        result["sources"] = [str(s).strip() for s in sources if s and str(s).strip()]

    # min_relevance: float [0..1]. Inválido → ignora.
    if "min_relevance" in data:
        try:
            mr = float(data["min_relevance"])
            if 0.0 <= mr <= 1.0:
                result["min_relevance"] = mr
        except (TypeError, ValueError):
            pass

    # max_age_days: int positivo. Inválido → ignora.
    if "max_age_days" in data:
        try:
            md = int(data["max_age_days"])
            if md > 0:
                result["max_age_days"] = md
        except (TypeError, ValueError):
            pass

    # cite_sources: bool. Wave 3 (citações opcionais).
    if "cite_sources" in data:
        result["cite_sources"] = bool(data["cite_sources"])

    return result


def _normalize_yaml11_bool_keys(obj):
    """YAML 1.1 (pyyaml) converte 'on'/'off'/'yes'/'no' para bool nas chaves.
    No domínio de API bindings essas keys são sempre strings — reverto aqui
    para evitar surpresa em retry.on, request.headers['off'] etc.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k is True:
                new_k = "on"
            elif k is False:
                new_k = "off"
            else:
                new_k = k
            out[new_k] = _normalize_yaml11_bool_keys(v)
        return out
    if isinstance(obj, list):
        return [_normalize_yaml11_bool_keys(x) for x in obj]
    return obj


def _parse_execution_mode(profile_text: str) -> str:
    """Parse mode from ## Execution Profile section content.

    Aceita formatos:
      mode: fast
      modo: rápido
      fast (keyword isolado)
    """
    lower = profile_text.lower().strip()

    # Formato key:value
    for line in profile_text.split('\n'):
        line_stripped = line.strip().lower()
        if line_stripped.startswith('mode:') or line_stripped.startswith('modo:'):
            val = line.split(':', 1)[1].strip().lower()
            # Mapear português
            mode_map = {
                'fast': 'fast', 'rápido': 'fast', 'rapido': 'fast',
                'standard': 'standard', 'padrão': 'standard', 'padrao': 'standard',
                'rigorous': 'rigorous', 'rigoroso': 'rigorous',
            }
            return mode_map.get(val, 'standard')

    # Keyword isolado
    if 'fast' in lower or 'rápido' in lower or 'rapido' in lower:
        return 'fast'
    if 'rigorous' in lower or 'rigoroso' in lower:
        return 'rigorous'

    return 'standard'


def _infer_execution_mode(parsed: ParsedSkill) -> str:
    """Auto-infere execution mode a partir das seções presentes no SKILL.md.

    Lógica:
    - Tem Evidence Policy substancial + Guardrails substanciais → rigorous
    - Tem Evidence Policy OU Guardrails → standard
    - Tem Tool Bindings sem Evidence Policy → fast (agente MCP típico)
    - Nenhum dos acima → fast
    """
    has_evidence = bool(
        parsed.evidence_policy
        and parsed.evidence_policy.strip()
        and len(parsed.evidence_policy.strip()) > 20
    )
    has_guardrails = bool(
        parsed.guardrails
        and parsed.guardrails.strip()
        and len(parsed.guardrails.strip()) > 50
    )
    has_tools = bool(
        parsed.tool_bindings
        and parsed.tool_bindings.strip()
        and len(parsed.tool_bindings.strip()) > 10
    )

    if has_evidence and has_guardrails:
        return 'rigorous'
    if has_evidence or has_guardrails:
        return 'standard'
    # MCP agents sem evidence policy → fast
    if has_tools and not has_evidence:
        return 'fast'
    return 'fast'


def _extract_sections(content: str) -> dict[str, str]:
    """Extrai todas seções H2 do markdown."""
    pattern = r"^##\s+(.+)$"
    matches = list(re.finditer(pattern, content, re.MULTILINE))
    sections = {}
    for i, match in enumerate(matches):
        name = match.group(1).strip()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        sections[name] = content[start:end].strip()
    return sections


def _section_to_attr(name: str) -> str:
    """Converte nome de seção para nome de atributo Python."""
    return name.lower().replace(" ", "_")


def skill_to_db_dict(parsed: ParsedSkill) -> dict:
    """Converte ParsedSkill para dicionário compatível com o banco."""
    fm = parsed.frontmatter
    return {
        "urn": fm.id,
        "name": parsed.name or "Untitled",
        "kind": fm.kind,
        "domain": fm.id.split(":")[2] if fm.id.count(":") >= 3 else "",
        "version": fm.version,
        "stability": fm.stability,
        "owner": fm.owner,
        "purpose": parsed.purpose,
        "activation_criteria": parsed.activation_criteria,
        "inputs_schema": parsed.inputs,
        "workflow": parsed.workflow,
        "tool_bindings": parsed.tool_bindings,
        "output_contract": parsed.output_contract,
        "failure_modes": parsed.failure_modes,
        "delegations": parsed.delegations,
        "compensation": parsed.compensation,
        "guardrails": parsed.guardrails,
        "budget": parsed.budget,
        "examples": parsed.examples,
        "telemetry": parsed.telemetry,
        "data_dependencies": parsed.data_dependencies,
        "model_constraints": parsed.model_constraints,
        "evidence_policy": parsed.evidence_policy,
        "gold_refs": parsed.gold_refs,
        "data_tables": parsed.data_tables,
        "raw_content": parsed.raw_content,
        "content_hash": parsed.content_hash,
    }


def validate_skill_references(parsed: ParsedSkill, existing_tools: list[str] = None) -> list[str]:
    """Valida referências externas do skill (tools, skills filhos)."""
    errors = []
    if parsed.tool_bindings and existing_tools is not None:
        try:
            bindings = json.loads(parsed.tool_bindings) if parsed.tool_bindings.startswith("[") else []
            for b in bindings:
                tool_name = b.get("name", "") if isinstance(b, dict) else str(b)
                if tool_name and tool_name not in existing_tools:
                    errors.append(f"Tool '{tool_name}' referenciada mas não registrada no Tool Registry")
        except (json.JSONDecodeError, TypeError):
            pass
    return errors