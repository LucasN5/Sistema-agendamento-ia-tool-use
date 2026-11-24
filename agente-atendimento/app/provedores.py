"""Provedor de "modelo" do agente: quem decide quais tools chamar e o que responder.

O provedor recebe o histórico (mensagens com blocos `text`, `tool_use` e `tool_result`) e
devolve a próxima resposta no mesmo formato. O único provedor é o `ProvedorLocal`, motor por
regras em `app/motor_local.py`: gratuito, offline e determinístico. O protocolo `Provedor`
permite injetar um provedor roteirizado nos testes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol


# ---------------------------------------------------------------- formato comum


@dataclass
class Bloco:
    """Bloco de conteúdo da resposta (`text` ou `tool_use`)."""

    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)

    def model_dump(self, **_: Any) -> dict[str, Any]:
        if self.type == "text":
            return {"type": "text", "text": self.text}
        return {"type": "tool_use", "id": self.id, "name": self.name, "input": self.input}


@dataclass
class Resposta:
    content: list[Any]
    stop_reason: str  # "end_turn" | "tool_use"


def texto(t: str) -> Bloco:
    return Bloco(type="text", text=t)


def chamada(nome: str, **entrada: Any) -> Bloco:
    return Bloco(type="tool_use", id=f"toolu_{uuid.uuid4().hex[:20]}", name=nome, input=entrada)


def resposta(*blocos: Bloco) -> Resposta:
    stop = "tool_use" if any(b.type == "tool_use" for b in blocos) else "end_turn"
    return Resposta(content=list(blocos), stop_reason=stop)


class Provedor(Protocol):
    nome: str

    def gerar(self, messages: list[dict[str, Any]]) -> Any:
        """Devolve um objeto com `.content` e `.stop_reason`."""
        ...


def _tipo(bloco: Any) -> str | None:
    return bloco.get("type") if isinstance(bloco, dict) else getattr(bloco, "type", None)


def _campo(bloco: Any, nome: str, padrao: Any = None) -> Any:
    return bloco.get(nome, padrao) if isinstance(bloco, dict) else getattr(bloco, nome, padrao)


# ---------------------------------------------------------------- local (regras)


class ProvedorLocal:
    """Motor por regras: gratuito, offline e determinístico."""

    nome = "local"

    def gerar(self, messages: list[dict[str, Any]]) -> Resposta:
        from app.motor_local import decidir

        return decidir(messages)


# ---------------------------------------------------------------- fábrica


def criar_provedor() -> Provedor:
    """Provedor padrão do agente: o motor local por regras."""
    return ProvedorLocal()
