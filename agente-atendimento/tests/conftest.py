"""Fixtures compartilhadas: estado do mock zerado, data fixa e provedor roteirizado."""

from __future__ import annotations

import copy
import itertools
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db, tools  # noqa: E402

HOJE = date(2026, 11, 24)


@pytest.fixture(autouse=True)
def estado_limpo(monkeypatch: pytest.MonkeyPatch):
    """Cada teste começa sem solicitações e com a data fixa."""
    pedidos = copy.deepcopy(db.PEDIDOS)
    db.SOLICITACOES.clear()
    monkeypatch.setattr(db, "_seq_protocolo", itertools.count(1))
    monkeypatch.setattr(tools, "_hoje", lambda: HOJE)
    yield
    db.SOLICITACOES.clear()
    db.PEDIDOS.clear()
    db.PEDIDOS.update(pedidos)


# ---------------------------------------------------------------- provedor roteirizado


def texto(t: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=t)


_ids = itertools.count(1)


def tool(nome: str, **entrada: Any) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=f"toolu_{next(_ids):04d}", name=nome, input=entrada)


def resposta(*blocos: SimpleNamespace) -> SimpleNamespace:
    stop = "tool_use" if any(b.type == "tool_use" for b in blocos) else "end_turn"
    return SimpleNamespace(content=list(blocos), stop_reason=stop)


class ProvedorRoteirizado:
    """Substitui o motor local devolvendo respostas roteirizadas e registrando cada chamada."""

    nome = "roteirizado"

    def __init__(self, roteiro: list[SimpleNamespace]) -> None:
        self.roteiro = list(roteiro)
        self.chamadas: list[dict[str, Any]] = []

    def gerar(self, messages: list[dict[str, Any]]) -> SimpleNamespace:
        self.chamadas.append({"messages": copy.deepcopy(messages)})
        if not self.roteiro:
            raise AssertionError("O agente chamou o provedor mais vezes do que o roteiro previa")
        return self.roteiro.pop(0)
