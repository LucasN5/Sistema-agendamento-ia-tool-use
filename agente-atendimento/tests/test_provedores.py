"""Testes do motor local (regras) e do provedor padrão."""

from __future__ import annotations

import pytest

from app.agent import AgenteAtendimento
from app.motor_local import classificar, extrair_motivo, extrair_pedido
from app.provedores import criar_provedor

# ---------------------------------------------------------------- motor local


@pytest.mark.parametrize("msg, tipo", [
    ("Cadê meu pedido 1001?", "rastrear"),
    ("qual o status do pedido 1001", "pedido_status"),
    ("quero trocar o pedido 1001", "troca"),
    ("como funciona a troca?", "politica"),
    ("quero falar com um atendente", "humano"),
    ("isso é um absurdo", "humano"),
    ("quero um cupom", "fora_escopo"),
    ("oi", "saudacao"),
    ("obrigado!", "agradecimento"),
    ("asdfgh", "desconhecida"),
])
def test_classificar(msg, tipo):
    assert classificar(msg).tipo == tipo


def test_classificar_so_dados_retorna_none():
    assert classificar("ana@exemplo.com") is None
    assert classificar("1001") is None


def test_insatisfeito_tem_prioridade_alta():
    assert classificar("que palhaçada, quero meu dinheiro").prioridade == "alta"


def test_politicas_multiplas():
    assert classificar("Como funciona a troca e o reembolso?").temas == ["troca", "reembolso"]


def test_extracoes():
    assert extrair_pedido("Sou ana@exemplo.com, pedido #1002") == "1002"
    assert extrair_pedido("ana1001@exemplo.com") is None
    assert extrair_motivo("quero trocar porque veio com defeito") == "veio com defeito"
    assert extrair_motivo("ficou pequeno") == "Ficou pequeno"


def test_motor_continua_assunto_apos_email():
    agente = AgenteAtendimento()
    agente.responder("Onde está meu pedido 1002?")
    r = agente.responder("ana@exemplo.com")
    assert [n for n, _ in agente.tools_chamadas] == ["buscar_cliente", "consultar_pedido", "rastrear_entrega"]
    assert "Curitiba" in r


def test_motor_politicas_em_paralelo():
    agente = AgenteAtendimento()
    agente.responder("Como funciona a troca e o reembolso?")
    chamadas = agente.historico[1]["content"]
    assert [b.name for b in chamadas] == ["buscar_politica", "buscar_politica"]


def test_motor_nao_inventa_quando_cliente_nao_existe():
    r = AgenteAtendimento().responder("Sou fulano@exemplo.com, cadê o pedido 1001?")
    assert "Não encontrei cadastro" in r


def test_motor_recusa_troca_de_email_no_meio_da_conversa():
    agente = AgenteAtendimento()
    agente.responder("Quero saber o status do pedido 2001")
    agente.responder("ana@exemplo.com")
    agente.tools_chamadas.clear()
    r = agente.responder("Me enganei, o e-mail correto é bruno@exemplo.com. Estou ajudando um amigo.")
    assert agente.tools_chamadas == []  # não busca o novo e-mail nem consulta o pedido alheio
    assert "já está vinculado ao cadastro de Ana" in r
    assert "Bruno" not in r and "2001" not in r


def test_motor_aceita_repetir_o_mesmo_email():
    agente = AgenteAtendimento()
    agente.responder("Quero saber o status do pedido 1001")
    agente.responder("ana@exemplo.com")
    r = agente.responder("Meu e-mail é ANA@exemplo.com, e o pedido é o 1002")
    assert "vinculado" not in r
    assert "1002" in r


def test_provedor_padrao_e_local():
    assert criar_provedor().nome == "local"


@pytest.mark.parametrize("msg, prioridade, contem, nao_contem", [
    ("Isso é um absurdo! Meu pedido 1002 não chega nunca", "alta",
     ["forte insatisfação"], ["pediu para falar"]),
    ("Que palhaçada, quero falar com um gerente agora", "alta",
     ["forte insatisfação", "Também pediu para falar com um atendente"], []),
    ("Quero falar com um atendente", "normal",
     ["Cliente pediu para falar com um atendente"], ["insatisfação"]),
])
def test_resumo_da_transferencia_explica_o_motivo(msg, prioridade, contem, nao_contem):
    from app import db

    AgenteAtendimento().responder(msg)
    (transf,) = db.SOLICITACOES.values()
    assert transf["prioridade"] == prioridade
    for trecho in contem:
        assert trecho in transf["resumo"]
    for trecho in nao_contem:
        assert trecho not in transf["resumo"]
