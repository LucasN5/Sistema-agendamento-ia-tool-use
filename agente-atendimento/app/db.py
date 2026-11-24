"""Camada de dados mockada.

Para trocar por banco/API real, mantenha as assinaturas das funções de acesso
abaixo (obter_cliente_por_email, obter_pedido, ...) e substitua a implementação.
As tools em `tools.py` só conversam com estas funções, nunca com os dicionários.
"""

from __future__ import annotations

import copy
import itertools
import json
from datetime import datetime
from pathlib import Path
from typing import Any

CLIENTES: dict[str, dict[str, Any]] = {
    "C001": {"id": "C001", "nome": "Ana Souza", "email": "ana@exemplo.com"},
    "C002": {"id": "C002", "nome": "Bruno Lima", "email": "bruno@exemplo.com"},
    "C003": {"id": "C003", "nome": "Carla Mendes", "email": "carla@exemplo.com"},
}

PEDIDOS: dict[str, dict[str, Any]] = {
    "1001": {
        "id": "1001",
        "cliente_id": "C001",
        "status": "entregue",
        "data_compra": "2026-11-02",
        "data_entrega": "2026-11-08",
        "itens": [
            {"produto": "Tênis Corrida Pro", "tamanho": "39", "qtd": 1, "preco": 349.90},
            {"produto": "Meia Esportiva (3 pares)", "qtd": 1, "preco": 39.90},
        ],
        "total": 389.80,
        "codigo_rastreio": "BR123456789",
    },
    "1002": {
        "id": "1002",
        "cliente_id": "C001",
        "status": "em_transito",
        "data_compra": "2026-11-18",
        "data_entrega": None,
        "itens": [{"produto": "Mochila Urbana 25L", "qtd": 1, "preco": 199.00}],
        "total": 199.00,
        "codigo_rastreio": "BR987654321",
    },
    "2001": {
        "id": "2001",
        "cliente_id": "C002",
        "status": "processando",
        "data_compra": "2026-11-22",
        "data_entrega": None,
        "itens": [{"produto": "Fone Bluetooth X2", "qtd": 1, "preco": 259.00}],
        "total": 259.00,
        "codigo_rastreio": None,
    },
    "2002": {
        "id": "2002",
        "cliente_id": "C002",
        "status": "entregue",
        "data_compra": "2026-10-10",
        "data_entrega": "2026-10-15",
        "itens": [{"produto": "Camiseta Dry Fit", "tamanho": "M", "qtd": 2, "preco": 79.90}],
        "total": 159.80,
        "codigo_rastreio": "BR555000111",
    },
}

POLITICAS: dict[str, str] = {
    "troca": (
        "Trocas podem ser solicitadas em até 30 dias corridos após a entrega, "
        "para produtos sem sinais de uso e com etiqueta. A primeira troca tem frete grátis. "
        "Após a abertura, o cliente recebe por e-mail um código de postagem válido por 7 dias."
    ),
    "frete": (
        "Frete grátis para compras acima de R$ 299,00. Prazo padrão de 3 a 7 dias úteis "
        "para capitais e de 5 a 12 dias úteis para demais localidades, contados após a "
        "confirmação do pagamento."
    ),
    "reembolso": (
        "Reembolsos são processados em até 10 dias úteis após o recebimento do produto "
        "devolvido no centro de distribuição. Cartão de crédito: estorno em até 2 faturas. "
        "Pix ou boleto: depósito na conta informada pelo cliente."
    ),
}

# Respostas mockadas da "API da transportadora".
RASTREIOS: dict[str, list[dict[str, str]]] = {
    "BR123456789": [
        {"data": "2026-11-03 10:12", "evento": "Objeto postado", "local": "São Paulo/SP"},
        {"data": "2026-11-06 08:40", "evento": "Em trânsito", "local": "Campinas/SP"},
        {"data": "2026-11-08 14:05", "evento": "Entregue ao destinatário", "local": "Ribeirão Preto/SP"},
    ],
    "BR987654321": [
        {"data": "2026-11-19 09:30", "evento": "Objeto postado", "local": "São Paulo/SP"},
        {"data": "2026-11-21 17:22", "evento": "Em trânsito", "local": "Curitiba/PR"},
    ],
    "BR555000111": [
        {"data": "2026-10-11 11:00", "evento": "Objeto postado", "local": "São Paulo/SP"},
        {"data": "2026-10-15 16:45", "evento": "Entregue ao destinatário", "local": "Belo Horizonte/MG"},
    ],
}

SOLICITACOES: dict[str, dict[str, Any]] = {}
_seq_protocolo = itertools.count(1)


# ---------------------------------------------------------------- acesso a dados


def obter_cliente_por_email(email: str) -> dict[str, Any] | None:
    """Retorna o cliente com o e-mail informado (case-insensitive) ou None."""
    alvo = email.strip().lower()
    for cliente in CLIENTES.values():
        if cliente["email"].lower() == alvo:
            return copy.deepcopy(cliente)
    return None


def obter_pedido(pedido_id: str) -> dict[str, Any] | None:
    """Retorna uma cópia do pedido ou None."""
    pedido = PEDIDOS.get(pedido_id.strip().lstrip("#"))
    return copy.deepcopy(pedido) if pedido else None


def obter_politica(tema: str) -> str | None:
    return POLITICAS.get(tema)


def consultar_transportadora(codigo_rastreio: str) -> list[dict[str, str]] | None:
    """Mock da API da transportadora."""
    eventos = RASTREIOS.get(codigo_rastreio.strip().upper())
    return copy.deepcopy(eventos) if eventos is not None else None


def registrar_solicitacao_troca(pedido_id: str, cliente_id: str, motivo: str) -> dict[str, Any]:
    """Grava uma solicitação de troca e devolve o registro com o protocolo TR-XXXXXX."""
    protocolo = f"TR-{next(_seq_protocolo):06d}"
    registro = {
        "protocolo": protocolo,
        "tipo": "troca",
        "pedido_id": pedido_id,
        "cliente_id": cliente_id,
        "motivo": motivo,
        "status": "aberta",
        "criado_em": datetime.now().isoformat(timespec="seconds"),
    }
    SOLICITACOES[protocolo] = registro
    return copy.deepcopy(registro)


def registrar_transferencia(resumo: str, prioridade: str, cliente_id: str | None) -> dict[str, Any]:
    """Coloca o atendimento na fila humana e devolve o registro com protocolo AT-XXXXXX."""
    protocolo = f"AT-{next(_seq_protocolo):06d}"
    registro = {
        "protocolo": protocolo,
        "tipo": "transferencia",
        "cliente_id": cliente_id,
        "resumo": resumo,
        "prioridade": prioridade,
        "status": "na_fila",
        "criado_em": datetime.now().isoformat(timespec="seconds"),
    }
    SOLICITACOES[protocolo] = registro
    return copy.deepcopy(registro)


def troca_existente(pedido_id: str) -> dict[str, Any] | None:
    """Retorna a solicitação de troca aberta para o pedido, se houver."""
    for s in SOLICITACOES.values():
        if s["pedido_id"] == pedido_id and s["status"] == "aberta":
            return copy.deepcopy(s)
    return None


# ---------------------------------------------------------------- persistência do mock (modo script)


def salvar_estado(caminho: Path) -> None:
    """Grava as solicitações em JSON, para o modo script (um processo por mensagem)."""
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(json.dumps(SOLICITACOES, ensure_ascii=False, indent=2), encoding="utf-8")


def carregar_estado(caminho: Path) -> None:
    """Recarrega as solicitações gravadas e continua a numeração dos protocolos."""
    global _seq_protocolo
    if not caminho.exists():
        return
    SOLICITACOES.clear()
    SOLICITACOES.update(json.loads(caminho.read_text(encoding="utf-8")))
    ultimo = max((int(p.split("-")[1]) for p in SOLICITACOES), default=0)
    _seq_protocolo = itertools.count(ultimo + 1)
