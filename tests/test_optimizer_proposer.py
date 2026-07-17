"""Propositor grounded de variantes (45.0.0, PR3b do arco Otimização).

Cobre:
1. Guardas puras: strip de literais de red flag (lição do PR2), detector de
   vazamento de gold case, parse tolerante da resposta do LLM.
2. Variante-controle determinística (com e sem skill) e resumos grounded
   (adversariais NUNCA aparecem nos exemplos; red flags stripped na fonte).
3. Rota POST /optimizer/propose: gate root/admin, 404 agente, 422 skill
   declarativa, 422 sem gold, happy-path com LLM mockado (controle sempre
   presente; variante com vazamento REJEITADA com aviso; aviso quando a rota
   do optimizer coincide com a do judge).
4. Papel 'optimizer' registrado no roteamento LLM.

Mocks nos módulos — sem DB/LLM reais, convenção da suíte.
"""
from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.routes.optimizer as opt
from app.optimizer.proposer import (
    build_control_variant,
    parse_proposer_response,
    strip_red_flag_literals,
    summarize_gold,
    summarize_last_run,
    variant_leaks_gold,
)


def _async(value):
    async def _fn(*a, **k):
        return value
    return _fn


# ═══ 1. Guardas puras ════════════════════════════════════════════════════

class TestGuardasPuras:
    def test_strip_red_flag_oculta_o_literal(self):
        out = strip_red_flag_literals([
            "state_mismatch (expected=Refuse, got=Recommend)",
            "red_flag='desconto de 100% garantido'",
        ])
        assert out[0].startswith("state_mismatch")
        assert "desconto" not in out[1]
        assert "[conteúdo proibido oculto]" in out[1]

    def test_leak_detector_pega_verbatim_e_ignora_frases_curtas(self):
        cases = [{"input_text": "minha internet caiu ontem à noite e não volta",
                  "expected_output": "reinicie o roteador pelo botão traseiro e aguarde 2 minutos"}]
        leak = "Sempre responda: reinicie o roteador pelo botão traseiro e aguarde 2 minutos."
        assert variant_leaks_gold(leak, cases) is True
        assert variant_leaks_gold("Seja objetivo e cite evidências.", cases) is False
        # fragmento curto (< _LEAK_MIN_CHARS) não caracteriza vazamento
        assert variant_leaks_gold("internet", [{"input_text": "internet"}]) is False

    def test_leak_detector_pega_copia_do_meio(self):
        """Review [21]/[31]: cópia verbatim do MEIO de um gabarito longo era
        invisível às janelas ancoradas — as deslizantes pegam."""
        gab = ("primeiro verifique se todos os LEDs do roteador estão acesos "
               "depois desligue o equipamento da tomada aguarde dois minutos "
               "e religue observando a ordem de estabilização das luzes")
        cases = [{"expected_output": gab}]
        meio = gab[60:125]  # 65 chars do MIOLO
        assert variant_leaks_gold(f"Instrua sempre: {meio}.", cases) is True

    def test_leak_detector_tolera_reformat_de_whitespace(self):
        gab = "reinicie o roteador pelo botão traseiro e aguarde dois minutos"
        variante = "Diga: reinicie o roteador\n  pelo botão traseiro e\naguarde dois minutos"
        assert variant_leaks_gold(variante, [{"expected_output": gab}]) is True

    def test_leak_detector_permite_eco_dos_exemplos_enviados(self):
        """Review [28]: ecoar o exemplo que NÓS enviamos no contexto é
        ilustração legítima — não pode ser rejeitado como vazamento."""
        inp = "minha internet caiu de novo hoje cedo e o modem pisca vermelho"
        cases = [{"input_text": inp}]
        variante = f'Ex.: quando o cliente disser "{inp}", diagnostique antes.'
        assert variant_leaks_gold(variante, cases,
                                  allow_fragments=(inp,)) is False
        assert variant_leaks_gold(variante, cases) is True  # sem allowlist

    def test_parse_resposta_json_limpa_fenced_e_lixo(self):
        ok = parse_proposer_response('{"system_prompt": "Você é X.", "rationale": "r"}')
        assert ok["system_prompt"] == "Você é X."
        fenced = parse_proposer_response(
            '```json\n{"system_prompt": "Y", "rationale": "z"}\n```')
        assert fenced["system_prompt"] == "Y"
        assert parse_proposer_response("desculpe, não consegui") is None
        assert parse_proposer_response('{"rationale": "sem prompt"}') is None

    def test_parse_cerca_json_com_prosa_em_volta(self):
        """Review [7]/[32]: o formato MAIS comum de resposta de LLM — cerca
        ```json + prosa depois — quebrava o parser local; o canônico
        (extract_args_json) resolve."""
        content = ('Aqui está a variante:\n```json\n'
                   '{"system_prompt": "Você é Z, objetivo.", "rationale": "r"}\n'
                   '```\nQualquer dúvida me chame {aqui}.')
        out = parse_proposer_response(content)
        assert out is not None
        assert out["system_prompt"] == "Você é Z, objetivo."


# ═══ 2. Controle determinística + resumos ════════════════════════════════

class TestControleEResumos:
    def test_controle_deriva_do_contrato_sem_llm(self):
        agent = {"name": "Esp. NOC"}
        sections = {
            "purpose": "Diagnosticar incidentes de rede.\nMais detalhes...",
            "inputs": '```json\n{"type": "object", "properties": {"cd_cliente": {"type": "string"}, "urgencia": {"type": "string"}}}\n```',
            "output_contract": '{"type": "object"}',
        }
        v = build_control_variant(agent, sections)
        assert v["kind"] == "control"
        assert "Esp. NOC" in v["system_prompt"]
        assert "Diagnosticar incidentes de rede." in v["system_prompt"]
        assert "cd_cliente" in v["system_prompt"]
        assert "contrato de saída" in v["system_prompt"]

    def test_controle_nao_achata_campos_aninhados(self):
        """Review [8]: a regex crua listava propriedades ANINHADAS como
        entradas de topo — o SSOT extract_inputs_schema não."""
        sections = {
            "inputs": ('```json\n{"type": "object", "properties": {'
                       '"filtros": {"type": "object", "properties": {'
                       '"data_inicio": {"type": "string"}}}}}\n```'),
        }
        v = build_control_variant({"name": "A"}, sections)
        assert "filtros" in v["system_prompt"]
        assert "data_inicio" not in v["system_prompt"]

    def test_controle_sem_skill_ainda_e_valida(self):
        v = build_control_variant({"name": "Solo"}, None)
        assert "Solo" in v["system_prompt"] and v["kind"] == "control"

    def test_resumo_gold_exclui_adversariais_dos_exemplos(self):
        cases = [
            {"input_text": "pergunta normal um", "case_type": "normal",
             "category": "a", "expected_state": "Recommend"},
            {"input_text": "TEXTO ADVERSARIAL SECRETO", "case_type": "adversarial",
             "category": "b", "expected_state": "Refuse"},
        ]
        s = summarize_gold(cases)
        assert s["total"] == 2 and s["adversarial"] == 1
        assert all("ADVERSARIAL" not in x for x in s["exemplos_de_entrada"])

    def test_resumo_last_run_strippa_red_flags_e_tolera_string(self):
        run = {"run_type": "baseline", "accuracy": 0.7, "gate_result": "rejected",
               "details": json.dumps([
                   {"case_id": "c1", "passed": False, "category": "t",
                    "expected_state": "Refuse", "actual_state": "Recommend",
                    "failure_reasons": ["red_flag='promoção secreta'"]},
                   {"case_id": "c2", "passed": True},
               ])}
        out = summarize_last_run(run)
        assert len(out["falhas"]) == 1
        assert "promoção" not in json.dumps(out, ensure_ascii=False)
        assert summarize_last_run(None) is None


# ═══ 3. Rota /optimizer/propose ══════════════════════════════════════════

def _client():
    app = FastAPI()
    app.include_router(opt.router)
    return TestClient(app, raise_server_exceptions=False)


_GOLD_CASES = [
    {"id": "g1", "input_text": "minha internet caiu de novo hoje cedo",
     "expected_output": "verifique os LEDs do roteador e reinicie o equipamento",
     "case_type": "normal", "category": "tec", "expected_state": "Recommend"},
]

_SKILL_MD = ("---\nid: urn:skill:x:y:1\nversion: 1.0.0\nkind: subagent\n"
             "owner: t\nstability: stable\n---\n# Skill: X\n## Purpose\np\n")


def _wire(monkeypatch, *, skill_raw=_SKILL_MD, gold=None, runs=None,
          llm_json=None, judge_route=("azure", "gpt-4o"), llm_fn=None):
    monkeypatch.setattr(opt, "require_role",
                        lambda *r: _async({"id": "u1", "role": "admin"}))
    monkeypatch.setattr(opt.agents_repo, "find_by_id",
                        _async({"id": "a1", "name": "Ag", "skill_id": "s1",
                                "system_prompt": "Você é o Ag."}))
    monkeypatch.setattr(opt.skills_repo, "find_by_id",
                        _async({"id": "s1", "raw_content": skill_raw}))
    monkeypatch.setattr(opt.gold_cases_repo, "find_all",
                        _async(gold if gold is not None else list(_GOLD_CASES)))
    run_filters = {}

    async def _find_runs(**kw):
        run_filters.update(kw)
        return runs or []

    monkeypatch.setattr(opt.eval_runs_repo, "find_all", _find_runs)

    async def _resolve(task, **kw):
        return ("azure", "gpt-5-opt") if task == "optimizer" else judge_route

    monkeypatch.setattr("app.llm_routing.resolve_llm_for_task", _resolve)

    ledger = []

    async def _ledger(**kw):
        ledger.append(kw)

    monkeypatch.setattr("app.core.cost_ledger.record_invocation_cost", _ledger)

    content = json.dumps(llm_json if llm_json is not None else
                         {"system_prompt": "Você é o Ag. Seja objetivo e cite evidências.",
                          "rationale": "encurtei e ancorei em evidência"})
    seen_prompts = []

    async def _default_llm(messages, provider, model, **kw):
        seen_prompts.append(messages[0]["content"])
        if kw.get("usage_sink") is not None:
            kw["usage_sink"].update({"provider": provider, "model": model,
                                     "usage": {"prompt_tokens": 100,
                                               "completion_tokens": 50}})
        return content, provider, model

    monkeypatch.setattr("app.routes.wizard._wizard_llm_complete",
                        llm_fn or _default_llm)
    return {"ledger": ledger, "run_filters": run_filters,
            "seen_prompts": seen_prompts}


_BODY = {"agent_id": "a1", "gold_version": "latest", "n_variants": 1}


class TestRotaPropose:
    def test_403_sem_papel(self, monkeypatch):
        _wire(monkeypatch)
        from fastapi import HTTPException as _E

        def _deny(*r):
            async def _dep(request):
                raise _E(403, "Permissão insuficiente")
            return _dep

        monkeypatch.setattr(opt, "require_role", _deny)
        assert _client().post("/api/v1/optimizer/propose", json=_BODY).status_code == 403

    def test_404_agente(self, monkeypatch):
        _wire(monkeypatch)
        monkeypatch.setattr(opt.agents_repo, "find_by_id", _async(None))
        assert _client().post("/api/v1/optimizer/propose", json=_BODY).status_code == 404

    def test_422_skill_declarativa(self, monkeypatch):
        _wire(monkeypatch, skill_raw=_SKILL_MD.replace(
            "stability: stable\n", "stability: stable\nexecution_mode: declarative\n"))
        r = _client().post("/api/v1/optimizer/propose", json=_BODY)
        assert r.status_code == 422 and "DECLARATIVA" in r.json()["detail"]

    def test_422_sem_gold(self, monkeypatch):
        _wire(monkeypatch, gold=[])
        r = _client().post("/api/v1/optimizer/propose", json=_BODY)
        assert r.status_code == 422 and "Golden" in r.json()["detail"]

    def test_happy_path_llm_mais_controle(self, monkeypatch):
        _wire(monkeypatch)
        r = _client().post("/api/v1/optimizer/propose", json=_BODY)
        assert r.status_code == 200, r.text
        body = r.json()
        kinds = [v["kind"] for v in body["variants"]]
        assert kinds == ["llm", "control"]
        assert body["variants"][0]["proposed_by"] == "azure/gpt-5-opt"
        assert body["context_summary"]["optimizer_route"] == "azure/gpt-5-opt"
        # rotas optimizer≠judge → sem aviso de Goodhart
        assert not any("judge" in w for w in body["warnings"])

    def test_variante_com_vazamento_e_rejeitada(self, monkeypatch):
        _wire(monkeypatch, llm_json={
            "system_prompt": "Responda sempre: verifique os LEDs do roteador "
                             "e reinicie o equipamento imediatamente.",
            "rationale": "memorizei o gabarito"})
        r = _client().post("/api/v1/optimizer/propose", json=_BODY)
        body = r.json()
        # só a controle sobra; aviso de vazamento presente
        assert [v["kind"] for v in body["variants"]] == ["control"]
        assert any("vazamento" in w for w in body["warnings"])

    def test_aviso_quando_optimizer_igual_judge(self, monkeypatch):
        _wire(monkeypatch, judge_route=("azure", "gpt-5-opt"))
        body = _client().post("/api/v1/optimizer/propose", json=_BODY).json()
        assert any("judge" in w and "Goodhart" in w for w in body["warnings"])

    def test_llm_lixo_degrada_para_controle(self, monkeypatch):
        """Review [14]: LLM devolve prosa sem JSON → 200 com a controle e
        aviso — nunca 500 num endpoint report-only."""

        async def _prosa(messages, provider, model, **kw):
            return "desculpe, hoje não consigo propor nada", provider, model

        _wire(monkeypatch, llm_fn=_prosa)
        r = _client().post("/api/v1/optimizer/propose", json=_BODY)
        assert r.status_code == 200, r.text
        body = r.json()
        assert [v["kind"] for v in body["variants"]] == ["control"]
        assert any("sem JSON utilizável" in w for w in body["warnings"])

    def test_falha_de_uma_variante_nao_derruba_as_demais(self, monkeypatch):
        """Review [24]/[35]: 503 na variante 2 não descarta a variante 1 já
        gerada e paga — degradação POR variante."""
        calls = {"n": 0}

        async def _flaky(messages, provider, model, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                from fastapi import HTTPException as _E
                raise _E(503, "provider fora do ar")
            if kw.get("usage_sink") is not None:
                kw["usage_sink"].update({"usage": {"prompt_tokens": 10,
                                                   "completion_tokens": 5}})
            return json.dumps({"system_prompt": "Variante boa e generalista.",
                               "rationale": "ok"}), provider, model

        _wire(monkeypatch, llm_fn=_flaky)
        r = _client().post("/api/v1/optimizer/propose",
                           json={**_BODY, "n_variants": 2})
        assert r.status_code == 200, r.text
        body = r.json()
        kinds = [v["kind"] for v in body["variants"]]
        assert kinds == ["llm", "control"]
        assert any("falha na geração" in w for w in body["warnings"])

    def test_custo_do_propositor_vai_ao_ledger(self, monkeypatch):
        """Review [1]: o arco é sobre custo VISÍVEL — a chamada do propositor
        grava invocation_costs com source='optimizer'."""
        wired = _wire(monkeypatch)
        _client().post("/api/v1/optimizer/propose", json=_BODY)
        assert len(wired["ledger"]) == 1
        row = wired["ledger"][0]
        assert row["source"] == "optimizer" and row["tokens_used"] == 150
        assert row["agent_id"] == "a1" and row["user_id"] == "u1"

    def test_goodhart_pos_fallback_e_avisado(self, monkeypatch):
        """Review [25]: primário do optimizer caiu e o fallback aterrissou no
        modelo do JUDGE — o aviso usa o modelo REALMENTE usado."""

        async def _fellback(messages, provider, model, **kw):
            return json.dumps({"system_prompt": "Variante ok generalista.",
                               "rationale": "r"}), "azure", "gpt-4o"

        _wire(monkeypatch, judge_route=("azure", "gpt-4o"), llm_fn=_fellback)
        body = _client().post("/api/v1/optimizer/propose", json=_BODY).json()
        assert any("FALLBACK" in w and "judge" in w for w in body["warnings"])

    def test_tips_rotacionadas_com_n3(self, monkeypatch):
        """Review [16]: n_variants=3 usa 3 tips DISTINTAS (rotação)."""
        wired = _wire(monkeypatch)
        _client().post("/api/v1/optimizer/propose",
                       json={**_BODY, "n_variants": 3})
        import re as _re
        tips = {m.group(1) for p in wired["seen_prompts"]
                for m in [_re.search(r"DICA DE ESTILO desta variante \(([^)]+)\)", p)]
                if m}
        assert tips == {"concisa", "passo-a-passo", "persona"}

    def test_last_run_prefere_nao_experiment_e_filtra_gold(self, monkeypatch):
        """Reviews [17]/[36]: com runs mistos o feedback vem do baseline; e
        gold_version específico filtra a busca do último run."""
        runs = [
            {"run_type": "experiment", "accuracy": 0.9, "details": "[]",
             "gate_result": "skipped"},
            {"run_type": "baseline", "accuracy": 0.7, "details": "[]",
             "gate_result": "rejected"},
        ]
        wired = _wire(monkeypatch, runs=runs)
        body = _client().post(
            "/api/v1/optimizer/propose",
            json={**_BODY, "gold_version": "aurora-v1"}).json()
        assert body["context_summary"]["last_run"]["run_type"] == "baseline"
        assert wired["run_filters"].get("gold_version") == "aurora-v1"


# ═══ 4. Papel no roteamento + template ═══════════════════════════════════

def test_papel_optimizer_registrado():
    from app.llm_routing import DEFAULT_ROUTING, TASK_TYPES
    assert "optimizer" in TASK_TYPES
    assert "optimizer" in DEFAULT_ROUTING


def test_template_card_do_experimento():
    from pathlib import Path
    src = Path("app/templates/pages/harness.html").read_text(encoding="utf-8")
    assert 'data-testid="optimizer-card"' in src
    assert 'data-testid="optimizer-propose"' in src
    # Review [18]: pinar o CONTRATO do A/B — braço champion (POST sem
    # override) + challenger (com) + reuso do champion por alvo.
    assert "api.post('/api/v1/eval-runs/execute',base)" in src
    assert "config_overrides:{system_prompt:v.system_prompt}" in src
    assert "_champions" in src


def test_bucket_de_rate_limit_do_optimizer():
    """Review [2]: rota que dispara LLM cai no bucket apertado (workspace),
    não no genérico de 300/min."""
    from app.core.ratelimit import _bucket_for_path
    assert _bucket_for_path("/api/v1/optimizer/propose")[0] == "workspace"


def test_settings_ui_renderiza_papel_optimizer():
    """Reviews [13]/[19]/[26]: o aviso Goodhart manda o operador à tela de
    Roteamento — o papel TEM que existir lá."""
    from pathlib import Path
    src = Path("app/templates/pages/settings.html").read_text(encoding="utf-8")
    assert 'data-testid="routing-optimizer-select"' in src
    assert "routingForm.optimizer" in src
    assert "optimizer: r.routing?.optimizer || ''" in src
