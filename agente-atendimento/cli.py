"""Atendimento da Lia pelo terminal.

Modo interativo (chat):
    atendimento                         # ou: python cli.py
    atendimento --sessao maria          # retoma/grava a conversa "maria"

Modo script (uma mensagem por execução; a conversa fica salva entre execuções):
    atendimento "Meu e-mail é ana@exemplo.com, cadê o pedido 1002?" --sessao maria
    echo "Sim, pode abrir" | atendimento - --sessao maria
    atendimento "oi" --sessao maria --json   # saída em JSON (resposta + tools chamadas)
    atendimento --novo --sessao maria        # apaga a conversa e começa do zero

Dentro do chat: /novo reinicia a conversa, /sair (ou "sair") encerra.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path

from app import db
from app.agent import AgenteAtendimento

PASTA_SESSOES = Path(os.getenv("PASTA_SESSOES", Path(__file__).resolve().parent / ".sessoes"))
ARQUIVO_DADOS = PASTA_SESSOES / "_dados_mock.json"
SAUDACAO = "Olá! Sou a Lia, assistente da loja. Como posso ajudar?"


# ---------------------------------------------------------------- sessões em arquivo


def _arquivo(sessao: str) -> Path:
    if not re.fullmatch(r"[\w.-]{1,64}", sessao):
        raise SystemExit("Nome de sessão inválido: use letras, números, '.', '-' ou '_' (até 64).")
    return PASTA_SESSOES / f"{sessao}.json"


def carregar(sessao: str | None) -> AgenteAtendimento:
    """Abre a sessão salva (se houver) e o estado das solicitações mock."""
    db.carregar_estado(ARQUIVO_DADOS)
    if sessao and _arquivo(sessao).exists():
        estado = json.loads(_arquivo(sessao).read_text(encoding="utf-8"))
        return AgenteAtendimento.restaurar(estado)
    return AgenteAtendimento(sessao_id=sessao or "local")


def salvar(agente: AgenteAtendimento, sessao: str | None) -> None:
    db.salvar_estado(ARQUIVO_DADOS)
    if sessao:
        _arquivo(sessao).write_text(
            json.dumps(agente.exportar_estado(), ensure_ascii=False, indent=2), encoding="utf-8"
        )


def apagar(sessao: str) -> None:
    _arquivo(sessao).unlink(missing_ok=True)


# ---------------------------------------------------------------- modos


def modo_script(mensagem: str, sessao: str, saida_json: bool) -> int:
    """Responde uma única mensagem e grava a conversa."""
    agente = carregar(sessao)
    antes = len(agente.tools_chamadas)
    resposta = agente.responder(mensagem)
    salvar(agente, sessao)
    if saida_json:
        novas = [{"nome": n, "args": a} for n, a in agente.tools_chamadas[antes:]]
        print(json.dumps({"sessao": sessao, "resposta": resposta, "tools": novas}, ensure_ascii=False))
    else:
        print(resposta)
    return 0


def modo_interativo(sessao: str | None) -> int:
    """Loop de chat no terminal."""
    agente = carregar(sessao)
    if agente.historico:
        print(f"(retomando a sessão '{sessao}')")
    print(f"Lia: {SAUDACAO} (digite /sair para encerrar, /novo para reiniciar)")
    while True:
        try:
            mensagem = input("Você: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not mensagem:
            continue
        comando = mensagem.lower()
        if comando in {"sair", "/sair"}:
            break
        if comando == "/novo":
            if sessao:
                apagar(sessao)
            agente = AgenteAtendimento(sessao_id=sessao or "local")
            print(f"Lia: Conversa reiniciada. {SAUDACAO}")
            continue
        print(f"Lia: {agente.responder(mensagem)}")
        salvar(agente, sessao)
    print("Lia: Obrigada pelo contato! Até mais.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="atendimento", description="Atendimento ao cliente com a Lia.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__,
    )
    parser.add_argument("mensagem", nargs="?", help="mensagem única (modo script); use '-' para ler do stdin")
    parser.add_argument("--sessao", "-s", help="nome da sessão a retomar/gravar (padrão no modo script: 'padrao')")
    parser.add_argument("--novo", action="store_true", help="apaga a sessão antes de começar")
    parser.add_argument("--json", action="store_true", help="no modo script, imprime JSON")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "WARNING"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,  # stdout fica só com a resposta, para uso em scripts
    )

    mensagem = sys.stdin.read().strip() if args.mensagem == "-" else args.mensagem
    sessao = args.sessao or ("padrao" if mensagem or args.novo else None)

    if args.novo and sessao:
        apagar(sessao)
        if not mensagem:
            print(f"Sessão '{sessao}' reiniciada.")
            return 0
    if mensagem:
        return modo_script(mensagem, sessao, args.json)
    if args.mensagem == "-":
        parser.error("stdin vazio")
    return modo_interativo(sessao)


if __name__ == "__main__":
    raise SystemExit(main())
