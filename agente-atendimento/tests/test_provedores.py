"""Testes do motor local (regras) e da conversão de formato do provedor Ollama."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.agent import AgenteAtendimento
from app.config import config
from app.motor_local import classificar, extrair_motivo, extrair_pedido
from app.provedores import ErroProvedor, ProvedorOllama, criar_provedor
from app.tools import TOOLS

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


def test_provedor_padrao_e_local():
    assert criar_provedor(config).nome == "local"


# ---------------------------------------------------------------- Ollama (servidor HTTP falso)


class _OllamaFalso(BaseHTTPRequestHandler):
    respostas: list[dict] = []
    recebidos: list[dict] = []

    def do_POST(self):  # noqa: N802
        corpo = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.recebidos.append(corpo)
        dados = json.dumps(self.respostas.pop(0)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(dados)

    def log_message(self, *_):
        pass


@pytest.fixture
def ollama_falso():
    servidor = HTTPServer(("127.0.0.1", 0), _OllamaFalso)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    _OllamaFalso.respostas, _OllamaFalso.recebidos = [], []
    yield _OllamaFalso, f"http://127.0.0.1:{servidor.server_port}"
    servidor.shutdown()


def test_ollama_loop_completo(ollama_falso):
    import dataclasses

    falso, url = ollama_falso
    falso.respostas += [
        {"message": {"role": "assistant", "content": "",
                     "tool_calls": [{"function": {"name": "buscar_politica", "arguments": {"tema": "frete"}}}]},
         "prompt_eval_count": 50, "eval_count": 10},
        {"message": {"role": "assistant", "content": "Frete grátis acima de R$ 299,00."}},
    ]
    cfg = dataclasses.replace(config, provedor="ollama", ollama_url=url)
    agente = AgenteAtendimento(cfg=cfg)
    assert agente.responder("Qual o frete?") == "Frete grátis acima de R$ 299,00."

    enviado = falso.recebidos[1]
    assert enviado["messages"][0]["role"] == "system"
    assert enviado["messages"][2]["tool_calls"][0]["function"]["name"] == "buscar_politica"
    assert enviado["messages"][3]["role"] == "tool" and "R$ 299,00" in enviado["messages"][3]["content"]
    assert {t["function"]["name"] for t in enviado["tools"]} == {t["name"] for t in TOOLS}


def test_ollama_desligado_vira_mensagem_amigavel():
    import dataclasses

    cfg = dataclasses.replace(config, ollama_url="http://127.0.0.1:9", max_tentativas_api=1)
    with pytest.raises(ErroProvedor, match="Ollama"):
        ProvedorOllama(cfg).gerar("s", TOOLS, [{"role": "user", "content": "oi"}])
    assert "instabilidade" in AgenteAtendimento(provedor=ProvedorOllama(cfg)).responder("oi")
