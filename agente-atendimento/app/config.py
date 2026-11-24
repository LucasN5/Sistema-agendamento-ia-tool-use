"""Configuração lida do ambiente (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    """Parâmetros do agente. Todos podem ser sobrescritos via variáveis de ambiente."""

    # Loop e guardrails
    max_iteracoes: int = int(os.getenv("MAX_ITERACOES", "8"))
    max_mensagens_historico: int = int(os.getenv("MAX_MENSAGENS_HISTORICO", "40"))
    timeout_transportadora_s: float = float(os.getenv("TIMEOUT_TRANSPORTADORA_S", "5"))


config = Config()
