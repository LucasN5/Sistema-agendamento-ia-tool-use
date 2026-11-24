"""API HTTP da Lia (FastAPI).

Rodar: uvicorn app.api:app --reload
"""

from __future__ import annotations

import logging
import threading

from fastapi import FastAPI
from pydantic import BaseModel, Field

from app.agent import AgenteAtendimento

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="Lia - Atendimento")

# Uma conversa por sessão, em memória. Em produção, use Redis (ou outro store compartilhado)
# serializando `historico` e `contexto`, com TTL por sessão, para funcionar com vários workers.
_SESSOES: dict[str, AgenteAtendimento] = {}
_LOCKS: dict[str, threading.Lock] = {}
_LOCK_GLOBAL = threading.Lock()


class ChatEntrada(BaseModel):
    sessao_id: str = Field(min_length=1, max_length=100)
    mensagem: str = Field(min_length=1, max_length=4000)


class ChatSaida(BaseModel):
    resposta: str


def _sessao(sessao_id: str) -> tuple[AgenteAtendimento, threading.Lock]:
    with _LOCK_GLOBAL:
        if sessao_id not in _SESSOES:
            # Em produção: AgenteAtendimento(cliente_id_autenticado=<id do usuário logado>, ...)
            _SESSOES[sessao_id] = AgenteAtendimento(sessao_id=sessao_id)
            _LOCKS[sessao_id] = threading.Lock()
        return _SESSOES[sessao_id], _LOCKS[sessao_id]


@app.post("/chat", response_model=ChatSaida)
def chat(entrada: ChatEntrada) -> ChatSaida:
    """Envia uma mensagem na sessão e devolve a resposta da Lia."""
    agente, lock = _sessao(entrada.sessao_id)
    with lock:  # mensagens da mesma sessão são processadas em ordem
        return ChatSaida(resposta=agente.responder(entrada.mensagem))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
