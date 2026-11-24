"""Testes unitários das ferramentas e do dispatcher."""

from __future__ import annotations

import json
from types import SimpleNamespace

from app import db
from app.tools import (
    TOOLS,
    ContextoSessao,
    abrir_solicitacao_troca,
    buscar_cliente,
    buscar_politica,
    consultar_pedido,
    executar_tool,
    rastrear_entrega,
    transferir_para_humano,
)


def exec_json(nome: str, args: dict, ctx: ContextoSessao | None = None) -> dict:
    return json.loads(executar_tool(nome, args, ctx))


# ---------------------------------------------------------------- funções


def test_buscar_cliente_ok_e_case_insensitive():
    assert buscar_cliente("ANA@exemplo.com") == {"cliente_id": "C001", "nome": "Ana Souza"}


def test_buscar_cliente_inexistente():
    assert "erro" in buscar_cliente("ninguem@exemplo.com")


def test_consultar_pedido_do_cliente():
    r = consultar_pedido("1001", "C001")
    assert r["status"] == "entregue"
    assert r["codigo_rastreio"] == "BR123456789"
    assert r["total"] == 389.80


def test_consultar_pedido_de_outro_cliente_nao_vaza_dados():
    r = consultar_pedido("2001", "C001")
    assert r == {"erro": "Pedido não encontrado para este cliente."}
    # Mesma resposta de um pedido inexistente: não revela que o pedido existe.
    assert consultar_pedido("9999", "C001") == r


def test_rastrear_entrega_ok():
    r = rastrear_entrega("BR987654321")
    assert r["ultimo_evento"]["evento"] == "Em trânsito"


def test_rastrear_entrega_codigo_invalido():
    assert "erro" in rastrear_entrega("XX000000000")


def test_rastrear_entrega_timeout(monkeypatch):
    import time

    from app import tools

    monkeypatch.setattr(db, "consultar_transportadora", lambda c: time.sleep(0.5))
    monkeypatch.setattr(tools, "config", SimpleNamespace(timeout_transportadora_s=0.05))
    assert "não respondeu a tempo" in rastrear_entrega("BR123456789")["erro"]


def test_buscar_politica():
    assert "30 dias" in buscar_politica("troca")["politica"]
    assert "erro" in buscar_politica("garantia")


def test_abrir_troca_ok_gera_protocolo():
    r = abrir_solicitacao_troca("1001", "C001", "Tamanho ficou pequeno")
    assert r["protocolo"] == "TR-000001"
    assert db.SOLICITACOES["TR-000001"]["pedido_id"] == "1001"


def test_abrir_troca_pedido_nao_entregue():
    r = abrir_solicitacao_troca("1002", "C001", "Quero trocar")
    assert "entregues" in r["erro"]
    assert db.SOLICITACOES == {}


def test_abrir_troca_pedido_de_outro_cliente():
    r = abrir_solicitacao_troca("2002", "C001", "Quero trocar")
    assert r == {"erro": "Pedido não encontrado para este cliente."}
    assert db.SOLICITACOES == {}


def test_abrir_troca_fora_do_prazo():
    assert "Prazo de troca expirado" in abrir_solicitacao_troca("2002", "C002", "Quero trocar")["erro"]


def test_abrir_troca_duplicada():
    abrir_solicitacao_troca("1001", "C001", "Tamanho")
    assert "Já existe" in abrir_solicitacao_troca("1001", "C001", "Tamanho")["erro"]


def test_transferir_para_humano():
    r = transferir_para_humano("Cliente quer falar com atendente", "alta")
    assert r["protocolo"].startswith("AT-") and r["prioridade"] == "alta"


# ---------------------------------------------------------------- dispatcher


def test_dispatcher_tool_inexistente():
    assert exec_json("cancelar_tudo", {}) == {"erro": "Ferramenta inexistente: cancelar_tudo"}


def test_dispatcher_argumentos_invalidos():
    assert "Argumentos inválidos" in exec_json("buscar_politica", {"tema": "garantia"})["erro"]
    assert "Argumentos inválidos" in exec_json("buscar_cliente", {"email": "não é email"})["erro"]
    assert "Argumentos inválidos" in exec_json("consultar_pedido", {"pedido_id": "1001"})["erro"]


def test_dispatcher_json_sem_escape_ascii():
    saida = executar_tool("buscar_politica", {"tema": "reembolso"})
    assert "úteis" in saida


def test_dispatcher_captura_excecao(monkeypatch):
    def explode(_email):
        raise RuntimeError("banco fora do ar")

    monkeypatch.setattr(db, "obter_cliente_por_email", explode)
    assert "Falha interna" in exec_json("buscar_cliente", {"email": "ana@exemplo.com"})["erro"]


def test_schemas_validos():
    nomes = {t["name"] for t in TOOLS}
    assert nomes == {
        "buscar_cliente", "consultar_pedido", "rastrear_entrega",
        "buscar_politica", "abrir_solicitacao_troca", "transferir_para_humano",
    }
    for t in TOOLS:
        assert t["strict"] is True
        assert t["input_schema"]["additionalProperties"] is False


# ---------------------------------------------------------------- guardrails de sessão


def test_contexto_exige_identificacao_antes_do_pedido():
    ctx = ContextoSessao()
    assert "não identificado" in exec_json("consultar_pedido", {"pedido_id": "1001", "cliente_id": "C001"}, ctx)["erro"]


def test_contexto_injeta_cliente_id_da_sessao():
    ctx = ContextoSessao()
    exec_json("buscar_cliente", {"email": "ana@exemplo.com"}, ctx)
    assert ctx.cliente_id == "C001"
    # O modelo tenta passar o cliente dono do pedido 2001; o dispatcher usa o da sessão.
    r = exec_json("consultar_pedido", {"pedido_id": "2001", "cliente_id": "C002"}, ctx)
    assert r == {"erro": "Pedido não encontrado para este cliente."}


def test_contexto_autenticado_bloqueia_outro_email():
    ctx = ContextoSessao(cliente_id_autenticado="C001")
    assert "não corresponde" in exec_json("buscar_cliente", {"email": "bruno@exemplo.com"}, ctx)["erro"]
    assert ctx.cliente_id == "C001"


def test_contexto_troca_exige_confirmacao():
    ctx = ContextoSessao(cliente_id_autenticado="C001")
    args = {"pedido_id": "1001", "cliente_id": "C001", "motivo": "Tamanho"}

    r1 = exec_json("abrir_solicitacao_troca", args, ctx)
    assert r1["status"] == "confirmacao_necessaria"
    assert db.SOLICITACOES == {} and ctx.acao_pendente is not None

    # Sem confirmação marcada, chamar de novo ainda não grava.
    assert exec_json("abrir_solicitacao_troca", args, ctx)["status"] == "confirmacao_necessaria"
    assert db.SOLICITACOES == {}

    ctx.acao_confirmada = True  # o agente faz isso quando o cliente diz "sim"
    r2 = exec_json("abrir_solicitacao_troca", args, ctx)
    assert r2["protocolo"].startswith("TR-")
    assert ctx.acao_pendente is None


def test_contexto_troca_inelegivel_nao_pede_confirmacao():
    ctx = ContextoSessao(cliente_id_autenticado="C001")
    r = exec_json("abrir_solicitacao_troca", {"pedido_id": "1002", "cliente_id": "C001", "motivo": "x" * 5}, ctx)
    assert "entregues" in r["erro"]
    assert ctx.acao_pendente is None
