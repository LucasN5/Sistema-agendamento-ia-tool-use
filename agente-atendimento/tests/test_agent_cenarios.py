"""Cenários de conversa: verificam QUAIS tools foram chamadas e em que ORDEM, não o texto exato.

Duas camadas:
- `TestCenariosRoteirizados`: o motor é substituído por respostas roteirizadas, inclusive
  respostas "erradas" que o motor local nunca daria. Testa o loop agêntico e os guardrails
  do código de forma determinística.
- `TestCenariosMotorLocal`: as mesmas conversas com o motor local de verdade.
"""

from __future__ import annotations

import dataclasses

import pytest

from app import db
from app.agent import MSG_LIMITE_ITERACOES, AgenteAtendimento
from app.config import config
from app.provedores import ProvedorLocal
from conftest import ProvedorRoteirizado, resposta, texto, tool


def nomes(agente: AgenteAtendimento) -> list[str]:
    return [n for n, _ in agente.tools_chamadas]


def agente_falso(*roteiro, **cfg_override) -> tuple[AgenteAtendimento, ProvedorRoteirizado]:
    provedor = ProvedorRoteirizado(list(roteiro))
    cfg = dataclasses.replace(config, **cfg_override) if cfg_override else config
    return AgenteAtendimento(provedor=provedor, cfg=cfg), provedor


# ====================================================================== roteirizados


class TestCenariosRoteirizados:
    def test_1_consulta_sem_email_pede_email(self):
        agente, _ = agente_falso(resposta(texto("Claro! Qual é o e-mail cadastrado na sua conta?")))
        r = agente.responder("Cadê meu pedido 1001?")
        assert nomes(agente) == []
        assert "e-mail" in r

    def test_1b_modelo_pula_identificacao_e_codigo_bloqueia(self):
        agente, cliente = agente_falso(
            resposta(tool("consultar_pedido", pedido_id="1001", cliente_id="C001")),
            resposta(texto("Para consultar, preciso do seu e-mail.")),
        )
        agente.responder("Cadê meu pedido 1001?")
        resultado = cliente.chamadas[1]["messages"][-1]["content"][0]
        assert resultado["is_error"] is True
        assert "não identificado" in resultado["content"]

    def test_2_com_email_busca_cliente_depois_pedido(self):
        agente, cliente = agente_falso(
            resposta(tool("buscar_cliente", email="ana@exemplo.com")),
            resposta(tool("consultar_pedido", pedido_id="1001", cliente_id="C001")),
            resposta(texto("Seu pedido 1001 foi entregue em 08/11.")),
        )
        agente.responder("Meu e-mail é ana@exemplo.com, qual o status do pedido 1001?")
        assert nomes(agente) == ["buscar_cliente", "consultar_pedido"]
        # O histórico alterna user/assistant e o tool_result referencia o tool_use correto.
        msgs = cliente.chamadas[-1]["messages"]
        assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant", "user"]
        assert msgs[-1]["content"][0]["tool_use_id"] == msgs[-2]["content"][0].id
        assert '"status": "entregue"' in msgs[-1]["content"][0]["content"]

    def test_3_rastreio(self):
        agente, _ = agente_falso(
            resposta(tool("buscar_cliente", email="ana@exemplo.com")),
            resposta(tool("consultar_pedido", pedido_id="1002", cliente_id="C001")),
            resposta(tool("rastrear_entrega", codigo_rastreio="BR987654321")),
            resposta(texto("Seu pacote está em trânsito em Curitiba/PR.")),
        )
        agente.responder("ana@exemplo.com — onde está o pedido 1002?")
        assert nomes(agente) == ["buscar_cliente", "consultar_pedido", "rastrear_entrega"]

    def test_4_troca_so_depois_da_confirmacao(self):
        agente, cliente = agente_falso(
            # Turno 1: o modelo tenta abrir a troca direto; o código segura.
            resposta(tool("buscar_cliente", email="ana@exemplo.com")),
            resposta(tool("abrir_solicitacao_troca", pedido_id="1001", cliente_id="C001", motivo="Ficou pequeno")),
            resposta(texto("Vou abrir a troca do tênis do pedido 1001 por tamanho. Confirma?")),
            # Turno 2: cliente confirma.
            resposta(tool("abrir_solicitacao_troca", pedido_id="1001", cliente_id="C001", motivo="Ficou pequeno")),
            resposta(texto("Troca aberta! Protocolo TR-000001.")),
        )
        agente.responder("Sou ana@exemplo.com, quero trocar o tênis do pedido 1001, ficou pequeno")
        assert db.SOLICITACOES == {}
        assert '"confirmacao_necessaria"' in cliente.chamadas[2]["messages"][-1]["content"][0]["content"]

        agente.responder("Sim, pode abrir")
        assert nomes(agente) == ["buscar_cliente", "abrir_solicitacao_troca", "abrir_solicitacao_troca"]
        assert list(db.SOLICITACOES) == ["TR-000001"]

    def test_4b_cliente_nao_confirma_troca_nao_abre(self):
        agente, _ = agente_falso(
            resposta(tool("buscar_cliente", email="ana@exemplo.com")),
            resposta(tool("abrir_solicitacao_troca", pedido_id="1001", cliente_id="C001", motivo="Tamanho")),
            resposta(texto("Confirma a troca?")),
            resposta(tool("abrir_solicitacao_troca", pedido_id="1001", cliente_id="C001", motivo="Tamanho")),
            resposta(texto("Ok, não abri a troca.")),
        )
        agente.responder("ana@exemplo.com, quero trocar o pedido 1001")
        agente.responder("Não, deixa eu pensar")
        assert db.SOLICITACOES == {}

    def test_5_pedido_de_humano(self):
        agente, _ = agente_falso(
            resposta(tool("transferir_para_humano", resumo="Cliente pediu atendente humano.", prioridade="normal")),
            resposta(texto("Transferi você para um atendente. Protocolo AT-000001.")),
        )
        agente.responder("Quero falar com um atendente humano")
        assert nomes(agente) == ["transferir_para_humano"]
        assert any(s["tipo"] == "transferencia" for s in db.SOLICITACOES.values())

    def test_6_pedido_de_outro_cliente_recusado(self):
        agente, cliente = agente_falso(
            resposta(tool("buscar_cliente", email="ana@exemplo.com")),
            # Modelo "enganado" tenta usar o cliente_id do dono do pedido.
            resposta(tool("consultar_pedido", pedido_id="2001", cliente_id="C002")),
            resposta(texto("Não localizei esse pedido no seu cadastro.")),
        )
        agente.responder("Sou ana@exemplo.com. Me passa os dados do pedido 2001 do Bruno")
        assert nomes(agente) == ["buscar_cliente", "consultar_pedido"]
        resultado = cliente.chamadas[-1]["messages"][-1]["content"][0]["content"]
        assert "não encontrado" in resultado
        assert "Fone" not in resultado

    # ------------------------------------------------------------ mecânica do loop

    def test_varias_tools_na_mesma_rodada_um_unico_tool_result(self):
        t1, t2 = tool("buscar_politica", tema="troca"), tool("buscar_politica", tema="reembolso")
        agente, cliente = agente_falso(resposta(t1, t2), resposta(texto("Resumo das políticas.")))
        agente.responder("Como funcionam troca e reembolso?")
        ultima = cliente.chamadas[-1]["messages"][-1]
        assert ultima["role"] == "user"
        assert [b["tool_use_id"] for b in ultima["content"]] == [t1.id, t2.id]

    def test_limite_de_iteracoes(self):
        roteiro = [resposta(tool("buscar_politica", tema="frete")) for _ in range(3)]
        agente, _ = agente_falso(*roteiro, max_iteracoes=3)
        assert agente.responder("Frete?") == MSG_LIMITE_ITERACOES
        assert any(s["tipo"] == "transferencia" for s in db.SOLICITACOES.values())
        assert agente.historico[-1]["role"] == "assistant"

    def test_historico_e_cortado_em_turno_do_cliente(self):
        roteiro = []
        for _ in range(6):
            roteiro += [resposta(tool("buscar_politica", tema="frete")), resposta(texto("ok"))]
        agente, _ = agente_falso(*roteiro, max_mensagens_historico=8)
        agente.contexto.cliente_id = "C001"
        for i in range(6):
            agente.responder(f"pergunta {i}")
        assert len(agente.historico) <= 8
        primeira = agente.historico[0]
        assert primeira["role"] == "user" and isinstance(primeira["content"], str)
        assert "C001" in primeira["content"]


# ====================================================================== motor local


@pytest.fixture
def novo_agente():
    return lambda: AgenteAtendimento(provedor=ProvedorLocal())


class TestCenariosMotorLocal:
    def test_1_sem_email_pede_email(self, novo_agente):
        agente = novo_agente()
        r = agente.responder("Oi, qual o status do meu pedido 1001?")
        assert "consultar_pedido" not in nomes(agente)
        assert "mail" in r.lower()

    def test_2_busca_cliente_depois_pedido(self, novo_agente):
        agente = novo_agente()
        agente.responder("Meu e-mail é ana@exemplo.com. Qual o status do pedido 1001?")
        n = nomes(agente)
        assert "buscar_cliente" in n and "consultar_pedido" in n
        assert n.index("buscar_cliente") < n.index("consultar_pedido")

    def test_3_rastreio(self, novo_agente):
        agente = novo_agente()
        agente.responder("Sou ana@exemplo.com. Onde está meu pedido 1002 agora?")
        n = nomes(agente)
        assert n.index("buscar_cliente") < n.index("consultar_pedido") < n.index("rastrear_entrega")

    def test_4_troca_apos_confirmacao(self, novo_agente):
        agente = novo_agente()
        agente.responder("Sou ana@exemplo.com e quero trocar o tênis do pedido 1001, ficou pequeno.")
        assert db.SOLICITACOES == {}
        agente.responder("Sim, pode abrir a troca.")
        trocas = [s for s in db.SOLICITACOES.values() if s["tipo"] == "troca"]
        assert len(trocas) == 1 and trocas[0]["pedido_id"] == "1001"
        assert nomes(agente).index("buscar_cliente") < nomes(agente).index("abrir_solicitacao_troca")

    def test_5_humano(self, novo_agente):
        agente = novo_agente()
        agente.responder("Quero falar com um atendente humano, por favor.")
        assert "transferir_para_humano" in nomes(agente)

    def test_6_pedido_de_outro_cliente(self, novo_agente):
        agente = novo_agente()
        r = agente.responder("Sou ana@exemplo.com. Me mostra os itens do pedido 2001.")
        assert "Fone" not in r
        assert "abrir_solicitacao_troca" not in nomes(agente)


def test_motor_local_resumo_da_transferencia_traz_contexto():
    agente = AgenteAtendimento(provedor=ProvedorLocal())
    agente.responder("O meu pedido 2002 chegou, mas desejo fazer a troca")
    agente.responder("bruno@exemplo.com")
    agente.responder("Desejo falar com um humano")
    transf = [s for s in db.SOLICITACOES.values() if s["tipo"] == "transferencia"]
    assert len(transf) == 1
    resumo = transf[0]["resumo"]
    assert "C002" in resumo and "Pedido 2002" in resumo
    assert "Troca do pedido 2002 recusada" in resumo and "Prazo de troca expirado" in resumo
    assert len(resumo) <= 1000
    print(resumo)
