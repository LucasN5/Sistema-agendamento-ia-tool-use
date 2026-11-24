"""Configuração lida do ambiente (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _bool(valor: str | None, padrao: bool) -> bool:
    if valor is None:
        return padrao
    return valor.strip().lower() in {"1", "true", "sim", "yes", "on"}


@dataclass(frozen=True)
class Config:
    """Parâmetros do agente. Todos podem ser sobrescritos via variáveis de ambiente."""

    # Quem faz o papel do modelo: local (regras, gratuito e offline), ollama (LLM local gratuito)
    # ou anthropic (pago, exige ANTHROPIC_API_KEY).
    provedor: str = os.getenv("PROVEDOR", "local")
    ollama_url: str = os.getenv("OLLAMA_URL", "http://localhost:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")

    # Só para PROVEDOR=anthropic
    anthropic_api_key: str | None = os.getenv("ANTHROPIC_API_KEY")
    model: str = os.getenv("MODEL", "claude-sonnet-5-5")
    max_tokens: int = int(os.getenv("MAX_TOKENS", "16000"))
    # Sonnet 5.5 usa thinking adaptativo por padrão; o effort controla a profundidade.
    effort: str = os.getenv("EFFORT", "medium")
    # Fallback do lado do servidor caso o modelo recuse (beta). Desligue com USAR_FALLBACK=false.
    usar_fallback: bool = _bool(os.getenv("USAR_FALLBACK"), True)

    # Loop e guardrails (valem para todos os provedores)
    max_iteracoes: int = int(os.getenv("MAX_ITERACOES", "8"))
    max_mensagens_historico: int = int(os.getenv("MAX_MENSAGENS_HISTORICO", "40"))
    max_tentativas_api: int = int(os.getenv("MAX_TENTATIVAS_API", "4"))
    timeout_api_s: float = float(os.getenv("TIMEOUT_API_S", "60"))
    timeout_transportadora_s: float = float(os.getenv("TIMEOUT_TRANSPORTADORA_S", "5"))


config = Config()
