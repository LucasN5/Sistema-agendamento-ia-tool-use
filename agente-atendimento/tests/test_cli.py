"""Testes do comando `atendimento` (modo script com sessão persistida em arquivo)."""

from __future__ import annotations

import io
import json

import pytest

import cli
from app import db
from app.provedores import ProvedorAnthropic
from conftest import ClienteFalso, resposta, texto, tool


@pytest.fixture
def ambiente(tmp_path, monkeypatch):
    """Sessões numa pasta temporária e o modelo substituído por um roteiro."""
    monkeypatch.setattr(cli, "PASTA_SESSOES", tmp_path)
    monkeypatch.setattr(cli, "ARQUIVO_DADOS", tmp_path / "_dados_mock.json")
    falso = ClienteFalso([])
    monkeypatch.setattr("app.agent.criar_provedor", lambda cfg: ProvedorAnthropic(cfg, falso))
    return falso, tmp_path


def rodar(capsys, *args: str) -> dict:
    assert cli.main(list(args)) == 0
    return json.loads(capsys.readouterr().out)


def test_troca_em_duas_execucoes_separadas(ambiente, capsys):
    falso, pasta = ambiente
    falso.roteiro += [
        resposta(tool("buscar_cliente", email="ana@exemplo.com")),
        resposta(tool("abrir_solicitacao_troca", pedido_id="1001", cliente_id="C001", motivo="Ficou pequeno")),
        resposta(texto("Confirma a troca do pedido 1001?")),
    ]
    r1 = rodar(capsys, "Sou ana@exemplo.com, quero trocar o pedido 1001", "--sessao", "t", "--json")
    assert [t["nome"] for t in r1["tools"]] == ["buscar_cliente", "abrir_solicitacao_troca"]
    assert db.SOLICITACOES == {}
    assert (pasta / "t.json").exists()

    # "Novo processo": memória zerada, tudo vem dos arquivos.
    db.SOLICITACOES.clear()
    falso.roteiro += [
        resposta(tool("abrir_solicitacao_troca", pedido_id="1001", cliente_id="C001", motivo="Ficou pequeno")),
        resposta(texto("Troca aberta: TR-000001.")),
    ]
    r2 = rodar(capsys, "Sim, pode abrir", "--sessao", "t", "--json")
    assert r2["resposta"] == "Troca aberta: TR-000001."
    assert list(db.SOLICITACOES) == ["TR-000001"]

    # O histórico restaurado foi enviado à API como dicts JSON, mantendo o tool_use_id certo.
    msgs = falso.chamadas[-1]["messages"]
    assert msgs[0]["content"] == "Sou ana@exemplo.com, quero trocar o pedido 1001"
    assert isinstance(msgs[1]["content"][0], dict)  # veio do arquivo
    assert msgs[2]["content"][0]["tool_use_id"] == msgs[1]["content"][0]["id"]

    # Protocolos continuam a numeração entre execuções.
    db.SOLICITACOES.clear()
    db.carregar_estado(pasta / "_dados_mock.json")
    assert next(db._seq_protocolo) == 2


def test_mensagem_pelo_stdin(ambiente, capsys, monkeypatch):
    falso, _ = ambiente
    falso.roteiro.append(resposta(texto("Qual é o seu e-mail?")))
    monkeypatch.setattr("sys.stdin", io.StringIO('Cadê meu pedido "1001"?\n'))
    assert cli.main(["-", "--sessao", "s"]) == 0
    assert capsys.readouterr().out.strip() == "Qual é o seu e-mail?"
    assert falso.chamadas[0]["messages"][0]["content"] == 'Cadê meu pedido "1001"?'


def test_novo_apaga_sessao(ambiente, capsys):
    falso, pasta = ambiente
    falso.roteiro.append(resposta(texto("Oi!")))
    rodar(capsys, "oi", "--sessao", "x", "--json")
    assert (pasta / "x.json").exists()
    assert cli.main(["--novo", "--sessao", "x"]) == 0
    assert not (pasta / "x.json").exists()


def test_nome_de_sessao_invalido(ambiente):
    with pytest.raises(SystemExit):
        cli.main(["oi", "--sessao", "../fora"])


def test_modo_script_gratuito_com_motor_local(tmp_path, monkeypatch, capsys):
    """Fluxo completo sem chave nem custo: duas execuções separadas, motor local."""
    monkeypatch.setattr(cli, "PASTA_SESSOES", tmp_path)
    monkeypatch.setattr(cli, "ARQUIVO_DADOS", tmp_path / "_dados_mock.json")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    r1 = rodar(capsys, "Sou ana@exemplo.com e quero trocar o pedido 1001, ficou pequeno", "-s", "g", "--json")
    assert "Confirma" in r1["resposta"]
    db.SOLICITACOES.clear()  # novo processo
    r2 = rodar(capsys, "sim", "-s", "g", "--json")
    assert "TR-000001" in r2["resposta"]
    assert [t["nome"] for t in r2["tools"]] == ["abrir_solicitacao_troca"]
