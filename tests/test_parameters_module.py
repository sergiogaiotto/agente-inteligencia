"""Módulo Parâmetros em Configurações (PR5 do arco LLM-as-Judge, 25.1.0).

- 19 parâmetros do Verifier/harness saem do env-only e viram editáveis na
  aba Configurações → Parâmetros (root/admin), padrão F6 (DB→env→get_settings
  por chamada = runtime sem restart), com o .env como fallback (não-selados);
- `require_role`: primeiro gate por ROLE reusável — aplicado ao
  PUT /api/v1/settings (antes QUALQUER autenticado podia sobrescrever
  credenciais da plataforma; o sumiço das abas era só cosmético);
- GET /settings/parameters: valores EFETIVOS + fonte (banco vs ambiente);
- UI: aba com save por DELTA (herdados do ambiente não viram registro no
  banco por acidente).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import (
    PARAMETER_UI_KEYS,
    Settings,
    _NON_MODEL_UI_KEYS,
    _SEALED_ENV_VARS,
    _UI_TO_ENV_MAP,
)


# ─── Contrato do mapa (molde do test_mcp_per_tool_setting) ──────────

class TestParameterKeysContract:
    def test_todas_as_chaves_no_mapa_ui_env(self):
        for k in PARAMETER_UI_KEYS:
            assert k in _UI_TO_ENV_MAP, f"{k} fora do _UI_TO_ENV_MAP"

    def test_nao_seladas_env_continua_fallback(self):
        # Parâmetro não é credencial/modelo — o .env vale quando o banco
        # não tem valor (retrocompat de instalações que configuravam por env).
        for k in PARAMETER_UI_KEYS:
            assert k in _NON_MODEL_UI_KEYS, f"{k} deveria ser não-selada"
            assert _UI_TO_ENV_MAP[k] not in _SEALED_ENV_VARS

    def test_settings_tem_todos_os_campos(self):
        for k in PARAMETER_UI_KEYS:
            assert k in Settings.model_fields, f"Settings sem campo {k}"

    def test_env_names_seguem_convencao(self):
        for k in PARAMETER_UI_KEYS:
            assert _UI_TO_ENV_MAP[k] == k.upper(), (
                f"{k} → {_UI_TO_ENV_MAP[k]} (esperado {k.upper()})"
            )


# ─── Validação de faixas no SettingsSave (422 nomeado) ──────────────

class TestSettingsSaveValidation:
    def test_faixas_invalidas_rejeitadas(self):
        from app.routes.dashboard import SettingsSave
        with pytest.raises(ValidationError):
            SettingsSave(verifier_production_sample_rate=1.5)
        with pytest.raises(ValidationError):
            SettingsSave(verifier_factuality_threshold=7)
        with pytest.raises(ValidationError):
            SettingsSave(harness_min_accuracy=-0.1)
        with pytest.raises(ValidationError):
            SettingsSave(verifier_max_concurrent_jobs=0)

    def test_valores_validos_passam_e_none_fica_unset(self):
        from app.routes.dashboard import SettingsSave
        s = SettingsSave(verifier_factuality_threshold=3.5)
        dumped = s.model_dump(exclude_unset=True)
        assert dumped == {"verifier_factuality_threshold": 3.5}


# ─── require_role — gate reusável ───────────────────────────────────

def _client_with_role(monkeypatch, role: str):
    from fastapi import FastAPI

    async def fake_require_user(request):
        return {"id": "u1", "role": role}
    monkeypatch.setattr("app.core.auth.require_user", fake_require_user)

    from app.routes.dashboard import router as dashboard_router
    app = FastAPI()
    app.include_router(dashboard_router)
    from fastapi.testclient import TestClient
    return TestClient(app, raise_server_exceptions=False)


class TestRequireRole:
    def test_role_fora_da_lista_recebe_403(self, monkeypatch):
        c = _client_with_role(monkeypatch, "comum")
        r = c.put("/api/v1/settings", json={"timezone": "America/Sao_Paulo"})
        assert r.status_code == 403
        assert "papel" in r.json()["detail"]

    def test_admin_passa_no_put_settings(self, monkeypatch):
        saved = {}

        async def fake_set_many(d):
            saved.update(d)

        async def fake_apply():
            return 1

        async def fake_audit(row):
            return {}
        import app.routes.dashboard as dash
        monkeypatch.setattr(dash.settings_store, "set_many", fake_set_many)
        monkeypatch.setattr("app.core.config.apply_settings_to_env", fake_apply)
        monkeypatch.setattr(dash.audit_repo, "create", fake_audit)

        c = _client_with_role(monkeypatch, "admin")
        r = c.put(
            "/api/v1/settings", json={"verifier_factuality_threshold": 3.5}
        )
        assert r.status_code == 200, r.text
        assert saved == {"verifier_factuality_threshold": "3.5"}

    def test_faixa_invalida_da_422(self, monkeypatch):
        c = _client_with_role(monkeypatch, "root")
        r = c.put(
            "/api/v1/settings", json={"verifier_production_sample_rate": 2}
        )
        assert r.status_code == 422

    def test_get_settings_gated(self, monkeypatch):
        # GET /settings expõe credenciais — root/admin apenas (25.1.0)
        async def fake_get_all():
            return {}
        import app.routes.dashboard as dash
        monkeypatch.setattr(dash.settings_store, "get_all", fake_get_all)
        c = _client_with_role(monkeypatch, "comum")
        assert c.get("/api/v1/settings").status_code == 403
        c2 = _client_with_role(monkeypatch, "admin")
        assert c2.get("/api/v1/settings").status_code == 200


# ─── DELETE /settings/parameters/{key} — restaurar padrão ───────────

class TestResetParameter:
    def test_role_comum_recebe_403(self, monkeypatch):
        c = _client_with_role(monkeypatch, "comum")
        r = c.delete("/api/v1/settings/parameters/verifier_max_tokens")
        assert r.status_code == 403

    def test_chave_fora_da_allowlist_da_400(self, monkeypatch):
        c = _client_with_role(monkeypatch, "root")
        # azure_key é credencial selada — NÃO redefinível por aqui
        r = c.delete("/api/v1/settings/parameters/azure_key")
        assert r.status_code == 400

    def test_reset_apaga_do_banco_e_do_env(self, monkeypatch):
        import os
        deleted = {}

        async def fake_delete(key):
            deleted["key"] = key
            return True

        async def fake_apply():
            return 1

        async def fake_audit(row):
            return {}
        import app.routes.dashboard as dash
        monkeypatch.setattr(dash.settings_store, "delete", fake_delete)
        monkeypatch.setattr("app.core.config.apply_settings_to_env", fake_apply)
        monkeypatch.setattr(dash.audit_repo, "create", fake_audit)
        os.environ["VERIFIER_MAX_TOKENS"] = "1234"  # resíduo a limpar

        c = _client_with_role(monkeypatch, "root")
        r = c.delete("/api/v1/settings/parameters/verifier_max_tokens")
        assert r.status_code == 200
        assert deleted["key"] == "verifier_max_tokens"
        # env var não-selada removida na mão (apply não a poparia)
        assert "VERIFIER_MAX_TOKENS" not in os.environ


# ─── Consistência UI ↔ backend (min/max ↔ ge/le) ───────────────────

def _selects_da_aba_parametros() -> dict:
    """Extrai {key: [valores das options]} dos type:'select' da REGIÃO
    paramGroups de settings.html.

    Falha FECHADO: se um select for escrito num formato que o extrator não
    reconhece (label antes de type, aspas duplas, espaço após ':', select sem
    options), a contagem crua de type:'select' denuncia a divergência em vez
    de o item escapar da paridade em silêncio. Escopo por REGIÃO, não por
    membership em PARAMETER_UI_KEYS — um select da aba fora da tupla é
    exatamente o bug que os testes de paridade devem acusar (lição #700/#721),
    não um caso a filtrar.
    """
    import re
    src = Path("app/templates/pages/settings.html").read_text(encoding="utf-8")
    region = src[src.index("paramGroups: ["):src.index("_paramMeta()")]
    matches = list(re.finditer(
        r"\{key:'([a-z0-9_]+)',\s*type:'select'.*?options:\[(.*?)\]", region, re.S
    ))
    raw = re.findall(r"type\s*:\s*['\"]select['\"]", region)
    assert len(raw) == len(matches), (
        f"{len(raw)} select(s) crus na aba Parâmetros mas só {len(matches)} no "
        "formato que o extrator reconhece ({key:'...', type:'select', ... "
        "options:[...]}) — escreva o item no formato canônico ou atualize o "
        "extrator; um select fora do padrão escaparia da paridade em silêncio."
    )
    return {m.group(1): re.findall(r"value:'([^']*)'", m.group(2)) for m in matches}


class TestUiRangesMatchBackend:
    def test_faixas_ui_batem_com_settings_save(self):
        import re
        from app.routes.dashboard import SettingsSave
        src = Path("app/templates/pages/settings.html").read_text(encoding="utf-8")
        # extrai {key:'...', type:'number', min:X, max:Y, ...} do paramGroups
        pat = re.compile(
            r"\{key:'([a-z0-9_]+)',\s*type:'number',\s*min:([\d.]+),\s*max:([\d.]+)"
        )
        found = {m.group(1): (float(m.group(2)), float(m.group(3)))
                 for m in pat.finditer(src)}
        assert found, "nenhum campo number extraído da aba Parâmetros"
        for key, (ui_min, ui_max) in found.items():
            field = SettingsSave.model_fields[key]
            metas = getattr(field, "metadata", [])
            ge = next((m.ge for m in metas if hasattr(m, "ge")), None)
            le = next((m.le for m in metas if hasattr(m, "le")), None)
            assert ge == ui_min, f"{key}: UI min {ui_min} != ge {ge}"
            assert le == ui_max, f"{key}: UI max {ui_max} != le {le}"

    def test_selects_ui_tem_enum_fechado_no_settings_save(self):
        """Selects escapavam da paridade (o teste acima só cobre type:'number')
        — a manqueira do precedente wizard_reasoning_effort (27.0.0): sem
        pattern no SettingsSave, QUALQUER string era aceita e o runtime tratava
        lixo como default em silêncio. Sela a classe (68.0.0): toda option do
        select da aba é aceita pelo modelo; valor fora do enum é 422; select
        da aba fora de PARAMETER_UI_KEYS é acusado (lição #700/#721)."""
        from app.routes.dashboard import SettingsSave
        found = _selects_da_aba_parametros()
        fora = sorted(set(found) - set(PARAMETER_UI_KEYS))
        assert not fora, (
            f"selects da aba Parâmetros fora de PARAMETER_UI_KEYS {fora} — "
            "renderizam na tela mas GET /settings/parameters não os carrega e "
            "toda a cadeia de paridade fica cega (lição #700/#721)."
        )
        # regressão do extrator: os 2 selects do grupo 🪄 existem hoje
        assert {"wizard_reasoning_effort", "wizard_verbosity"} <= set(found)
        for key, values in found.items():
            assert values, f"{key}: select sem options extraídas"
            for v in values:
                SettingsSave(**{key: v})  # toda option da UI é aceita
            with pytest.raises(ValidationError, match=key):
                SettingsSave(**{key: "valor-fora-do-enum-xyz"})

    def test_enums_de_runtime_batem_com_as_options_da_ui(self):
        """3º elo da cadeia (classe #721): UI ↔ SettingsSave é coberto acima;
        aqui UI ↔ enums de RUNTIME do wizard. Option nova na UI+pattern sem o
        leitor/corpo correspondente viraria default silencioso (sanitização)
        — ou KeyError no request real, no caso dos corpos de prompt."""
        from app.routes.wizard import (
            _REASONING_EFFORT_VALUES,
            _WIZARD_PROMPT_BODIES,
            _WIZARD_VERBOSITY_VALUES,
        )
        found = _selects_da_aba_parametros()
        assert set(found["wizard_verbosity"]) == set(_WIZARD_VERBOSITY_VALUES)
        assert set(_WIZARD_PROMPT_BODIES) == set(_WIZARD_VERBOSITY_VALUES)
        # 'off' é o sentinela de desligado da UI (68.0.0; '' não sobrevive ao
        # apply, que poppa env falsy) — o leitor o sanitiza para None.
        assert set(found["wizard_reasoning_effort"]) == _REASONING_EFFORT_VALUES | {"off"}


# ─── GET /settings/parameters — efetivo + fonte ─────────────────────

class TestGetParameterSettings:
    def test_devolve_efetivo_e_fonte(self, monkeypatch):
        async def fake_get_all():
            return {"verifier_factuality_threshold": "4.0"}
        import app.routes.dashboard as dash
        monkeypatch.setattr(dash.settings_store, "get_all", fake_get_all)
        c = _client_with_role(monkeypatch, "root")
        r = c.get("/api/v1/settings/parameters")
        assert r.status_code == 200
        params = {p["key"]: p for p in r.json()["parameters"]}
        assert set(params) == set(PARAMETER_UI_KEYS)
        assert params["verifier_factuality_threshold"]["source"] == "banco"
        assert params["verifier_v2_enabled"]["source"] == "ambiente/padrão"
        # valor efetivo vem do get_settings (bool de verdade, não string)
        assert isinstance(params["verifier_v2_enabled"]["value"], bool)

    def test_role_comum_recebe_403(self, monkeypatch):
        c = _client_with_role(monkeypatch, "comum")
        assert c.get("/api/v1/settings/parameters").status_code == 403


# ─── UI (invariantes de template) ───────────────────────────────────

class TestParamsUi:
    def test_aba_parametros_gated_root_admin(self):
        src = Path("app/templates/pages/settings.html").read_text(encoding="utf-8")
        idx = src.index("settings-tab-params")
        last_if = src.rfind("{% if", 0, idx)
        gate = src[last_if:idx]
        assert "root" in gate and "admin" in gate

    def test_aba_carrega_efetivo_e_salva_delta(self):
        src = Path("app/templates/pages/settings.html").read_text(encoding="utf-8")
        assert "/api/v1/settings/parameters" in src
        body = src[src.index("async saveParams()"):src.index("/* ── Roteamento LLM")]
        # delta vs snapshot: herdados do ambiente não viram registro no banco
        assert "JSON.parse(this._paramsSnapshot" in body
        assert "v !== snap[k]" in body

    def test_grupos_cobrem_todas_as_chaves(self):
        src = Path("app/templates/pages/settings.html").read_text(encoding="utf-8")
        for k in PARAMETER_UI_KEYS:
            assert f"'{k}'" in src, f"aba Parâmetros sem campo {k}"

    def test_botao_restaurar_padrao_e_watch_guard(self):
        src = Path("app/templates/pages/settings.html").read_text(encoding="utf-8")
        assert "param-reset" in src                    # botão ↺ restaurar
        assert "resetParam(p.key, p.label)" in src
        assert "_paramsWatchInstalled" in src          # $watch registrado 1x
        # validação client-side de faixa antes do PUT
        assert "deve ficar entre" in src

    def test_base_html_renderiza_422_nomeado(self):
        src = Path("app/templates/layouts/base.html").read_text(encoding="utf-8")
        assert "_errDetail" in src
        assert "Array.isArray(d)" in src
        # os 3 verbos com corpo de erro roteiam pelo _errDetail — o DELETE do
        # "restaurar padrão" também surfacea 400/403 acionáveis (finding da
        # revisão pós-fix: api.del tinha ficado de fora).
        for verb in ("async post", "async put", "async del"):
            i = src.index(verb)
            line = src[i:src.index("\n", i)]
            assert "_errDetail" in line, f"api.{verb.split()[1]} não usa _errDetail"
