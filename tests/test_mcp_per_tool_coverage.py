"""Métrica de cobertura per-tool (39.x — item 3 PR5).

É o GATE OBJETIVO da remoção do legado {operation, query} (PR6): hoje o
critério "100% descoberto" não é medível. Estes testes pinam as decisões que
tornam a métrica honesta — em especial as que impedem que ela reporte
prontidão que não existe (oauth2/mTLS, frota truncada, 0/0 = 100%).
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.core.database as db
import app.routes.dashboard as dash


_DISC = json.dumps([{"name": "web_search", "inputSchema": {"type": "object"}}])

_URL = "/api/v1/tools/per-tool-coverage"


def _tool(tid, **over):
    base = {"id": tid, "name": f"conn-{tid}", "mcp_server": "http://mcp:3001",
            "discovered_tools": _DISC, "per_tool_mode": "inherit",
            "auth_requirements": ""}
    base.update(over)
    return base


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(dash.router)
    # A rota é gateada só no backfill; a métrica segue o padrão dos irmãos
    # de /tools (sem Depends) — nada a sobrescrever aqui.
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def fleet(monkeypatch):
    """Instala uma frota fake: find_all pagina de verdade, count devolve o total."""
    def _install(rows, total=None):
        async def _find_all(limit=100, offset=0, **filters):
            return rows[offset:offset + limit]

        async def _count(**filters):
            return len(rows) if total is None else total

        monkeypatch.setattr(db.tools_repo, "find_all", _find_all)
        monkeypatch.setattr(db.tools_repo, "count", _count)
    return _install


class TestOrdemDeRota:
    def test_nao_e_capturada_pela_rota_parametrica(self, client, fleet):
        """`/tools/{tool_id}` viria antes e casaria tool_id='per-tool-coverage'
        → 404 "Tool não encontrada", fazendo a métrica parecer não-implementada."""
        fleet([_tool("a")])
        r = client.get(_URL)
        assert r.status_code == 200, r.text
        assert "com_discovered" in r.json()


class TestSemanticaDeCoberto:
    def test_descoberto_com_modo_off_conta_como_coberto(self, client, fleet, monkeypatch):
        """A métrica mede PRONTIDÃO, não adoção. Um conector que optou por sair
        do per-tool hoje SOBREVIVE à remoção do legado — não pode travar o gate."""
        monkeypatch.setenv("MCP_PER_TOOL_ENABLED", "0")
        fleet([_tool("a", per_tool_mode="off")])
        body = client.get(_URL).json()
        assert body["com_discovered"] == 1
        assert body["sem_discovered"] == []
        assert body["cobertura_pct"] == 100.0
        assert body["pronto_para_remocao_legado"] is True
        # ...mas HOJE ele está no legado — e o badge precisa disso.
        assert body["em_legado_hoje"] == 1
        assert [r["id"] for r in body["legado_efetivo"]] == ["a"]

    def test_modo_on_sem_descoberta_e_pendencia(self, client, fleet, monkeypatch):
        """Hoje ele cai no legado em silêncio; a métrica torna isso visível."""
        monkeypatch.setenv("MCP_PER_TOOL_ENABLED", "1")
        fleet([_tool("a", per_tool_mode="on", discovered_tools=None)])
        body = client.get(_URL).json()
        assert body["com_discovered"] == 0
        assert body["sem_discovered"][0]["motivo"] == "nunca_descoberto"
        assert body["pronto_para_remocao_legado"] is False

    @pytest.mark.parametrize("raw", [None, "", "[]", "{", '[{"description":"x"}]'])
    def test_descoberta_vazia_ou_invalida_e_pendencia(self, client, fleet, raw):
        fleet([_tool("a", discovered_tools=raw)])
        body = client.get(_URL).json()
        assert body["com_discovered"] == 0
        assert len(body["sem_discovered"]) == 1


class TestMotivoDoBadge:
    """Os dois jeitos de estar no legado pedem AÇÕES diferentes — colapsá-los
    fazia o hint mandar 'mude para Herdar' um conector que já está em Herdar."""

    def test_herdando_global_off_e_distinto_de_off_explicito(self, client, fleet, monkeypatch):
        monkeypatch.setenv("MCP_PER_TOOL_ENABLED", "0")
        fleet([_tool("herda", per_tool_mode="inherit"),
               _tool("optou", per_tool_mode="off")])
        body = client.get(_URL).json()
        motivos = {r["id"]: r["motivo"] for r in body["legado_efetivo"]}
        assert motivos == {"herda": "global_off_herdando", "optou": "modo_off_explicito"}

    def test_sem_descoberta_usa_o_motivo_da_pendencia(self, client, fleet, monkeypatch):
        monkeypatch.setenv("MCP_PER_TOOL_ENABLED", "1")
        fleet([_tool("a", per_tool_mode="on", discovered_tools=None)])
        body = client.get(_URL).json()
        assert body["legado_efetivo"][0]["motivo"] == "nunca_descoberto"

    def test_per_tool_ativo_nao_entra_no_badge(self, client, fleet, monkeypatch):
        monkeypatch.setenv("MCP_PER_TOOL_ENABLED", "1")
        fleet([_tool("a", per_tool_mode="on")])
        body = client.get(_URL).json()
        assert body["legado_efetivo"] == []
        assert body["em_legado_hoje"] == 0


class TestPendenciaNomeada:
    @pytest.mark.parametrize("auth", ["oauth2", "mtls", "mTLS", "OAuth2"])
    def test_oauth2_e_mtls_sao_pendencia_nunca_coberto(self, client, fleet, auth):
        """O BACKFILL em lote pula esses conectores — mas o "Testar conexão"
        do conector descobre e persiste normalmente (_build_mcp_auth monta
        OAuth2 e mTLS). O motivo nomeia o backfill, não a plataforma: dizer
        "descoberta não implementada" mandaria o operador desistir de algo que
        ele resolve em dois cliques."""
        fleet([_tool("a", auth_requirements=auth, discovered_tools=None)])
        body = client.get(_URL).json()
        assert body["sem_discovered"][0]["motivo"] == "backfill_nao_cobre_auth"
        assert body["pendentes_por_motivo"] == {"backfill_nao_cobre_auth": 1}
        assert body["pronto_para_remocao_legado"] is False

    def test_oauth2_com_descoberta_conta_como_pronto(self, client, fleet):
        """Prova que o motivo acima é sobre o backfill: descoberto via Testar
        conexão, o conector oauth2 está pronto como qualquer outro."""
        fleet([_tool("a", auth_requirements="oauth2")])
        body = client.get(_URL).json()
        assert body["com_discovered"] == 1
        assert body["pronto_para_remocao_legado"] is True

    def test_sem_endpoint_tem_motivo_proprio(self, client, fleet):
        fleet([_tool("a", mcp_server="", discovered_tools=None)])
        body = client.get(_URL).json()
        assert body["sem_discovered"][0]["motivo"] == "sem_endpoint"
        assert body["sem_discovered"][0]["transport"] == "desconhecido"


class TestTransporte:
    def test_derivado_do_prefixo_nao_da_coluna_mentirosa(self, client, fleet):
        """`mcp_server_type` tem default 'http' e ninguém a escreve — todo
        conector stdio nasce marcado 'http'. O transporte real vem do prefixo."""
        fleet([_tool("a", mcp_server="npx -y @pkg/servidor", mcp_server_type="http",
                     discovered_tools=None)])
        body = client.get(_URL).json()
        assert body["sem_discovered"][0]["transport"] == "stdio"
        assert body["pendentes_por_transporte"] == {"stdio": 1}

    def test_eixos_transporte_e_motivo_sao_separados(self, client, fleet):
        """oauth2 é AUTH, não transporte — colapsar os eixos sumiria com o
        conector HTTP+oauth2 da contagem por transporte."""
        fleet([_tool("a", auth_requirements="oauth2", discovered_tools=None),
               _tool("b", mcp_server="npx -y x", discovered_tools=None)])
        body = client.get(_URL).json()
        assert body["pendentes_por_transporte"] == {"http": 1, "stdio": 1}
        assert body["pendentes_por_motivo"] == {
            "backfill_nao_cobre_auth": 1, "nunca_descoberto": 1,
        }


class TestPaginacaoEHonestidade:
    def test_pagina_alem_do_limite_default(self, client, fleet):
        """`find_all` tem LIMIT default 100. Um gate que mede 100 de 250 e diz
        "100% coberto" é pior que gate nenhum."""
        fleet([_tool(f"t{i}") for i in range(250)])
        body = client.get(_URL).json()
        assert body["scanned"] == 250
        assert body["total"] == 250
        assert body["truncated"] is False
        assert body["com_discovered"] == 250

    def test_total_e_a_frota_real_scanned_e_a_amostra(self, client, fleet):
        """Devolver a mesma variável nos dois campos prometeria dois
        significados e entregaria um — e `truncated` viraria adivinhação."""
        rows = [_tool(f"t{i}") for i in range(30)]
        fleet(rows, total=500)      # o banco tem 500; a varredura pegou 30
        body = client.get(_URL).json()
        assert body["scanned"] == 30
        assert body["total"] == 500
        assert body["truncated"] is True
        assert body["pronto_para_remocao_legado"] is False

    def test_truncated_quando_estoura_max_pages(self, client, fleet):
        fleet([_tool(f"t{i}") for i in range(500)])
        body = client.get(_URL + "?max_pages=2&page_size=10").json()
        assert body["scanned"] == 20
        assert body["truncated"] is True
        # Truncado NUNCA autoriza a remoção — medir parte da frota não é medir.
        assert body["pronto_para_remocao_legado"] is False

    def test_total_zero_nao_reporta_100_pct(self, client, fleet):
        """0/0 = "100% coberto" é a falsa confiança que um gate não pode herdar."""
        fleet([])
        body = client.get(_URL).json()
        assert body["total"] == 0
        assert body["cobertura_pct"] is None
        assert body["pronto_para_remocao_legado"] is False

    @pytest.mark.parametrize("qs,esperado", [
        ("?page_size=-1", 422),      # LIMIT negativo estourava 500 no Postgres
        ("?page_size=0", 422),
        ("?max_pages=0", 422),
        ("?max_pages=-5", 422),
        ("?page_size=999999", 422),  # sem teto, varre a frota inteira num SELECT
    ])
    def test_params_invalidos_sao_422_nao_500(self, client, fleet, qs, esperado):
        fleet([_tool("a")])
        assert client.get(_URL + qs).status_code == esperado

    def test_count_indisponivel_nao_derruba_a_metrica(self, client, fleet, monkeypatch):
        fleet([_tool("a")])

        async def _boom(**kw):
            raise RuntimeError("count falhou")
        monkeypatch.setattr(db.tools_repo, "count", _boom)
        body = client.get(_URL).json()
        assert body["total"] == 1 and body["truncated"] is False


class TestSegredo:
    def test_payload_nao_vaza_endpoint_nem_auth(self, client, fleet):
        """`mcp_server` pode carregar credencial em query string; `_strip_secrets_
        from_tool` não passa por aqui. A métrica só devolve o necessário p/ agir."""
        fleet([_tool("a", mcp_server="https://mcp.io/sse?apiKey=SUPER_SECRETO",
                     auth_token="tok-secreto", discovered_tools=None)])
        raw = client.get(_URL).text
        assert "SUPER_SECRETO" not in raw
        assert "tok-secreto" not in raw
        assert "mcp_server" not in raw


class TestGateDoPR6:
    def test_true_so_com_zero_pendencias(self, client, fleet):
        fleet([_tool("a"), _tool("b")])
        assert client.get(_URL).json()["pronto_para_remocao_legado"] is True

        fleet([_tool("a"), _tool("b", discovered_tools=None)])
        body = client.get(_URL).json()
        assert body["pronto_para_remocao_legado"] is False
        assert body["cobertura_pct"] == 50.0
