"""Provedores de "modelo" para o agente: quem decide quais tools chamar e o que responder.

Todos recebem e devolvem o mesmo formato (mensagens no estilo da Messages API, com blocos
`text` e `tool_use`), então o loop, as tools e os guardrails não mudam ao trocar de provedor.

- `local` (padrão): motor por regras em `app/motor_local.py`. Gratuito, offline, sem chave.
- `ollama`: LLM local e gratuito via Ollama (https://ollama.com), com tool calling.
- `anthropic`: Claude via API (pago, exige ANTHROPIC_API_KEY). Opcional.
"""

from __future__ import annotations

import json
import logging
import random
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.config import Config

logger = logging.getLogger("agente.provedor")


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
class Uso:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class Resposta:
    content: list[Any]
    stop_reason: str  # "end_turn" | "tool_use" | "refusal" | "max_tokens"
    usage: Uso = field(default_factory=Uso)


def texto(t: str) -> Bloco:
    return Bloco(type="text", text=t)


def chamada(nome: str, **entrada: Any) -> Bloco:
    return Bloco(type="tool_use", id=f"toolu_{uuid.uuid4().hex[:20]}", name=nome, input=entrada)


def resposta(*blocos: Bloco, uso: Uso | None = None) -> Resposta:
    stop = "tool_use" if any(b.type == "tool_use" for b in blocos) else "end_turn"
    return Resposta(content=list(blocos), stop_reason=stop, usage=uso or Uso())


class ErroProvedor(Exception):
    """Falha do provedor após as tentativas (rede, limite, servidor fora do ar)."""


class Provedor(Protocol):
    nome: str

    def gerar(self, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]) -> Any:
        """Devolve um objeto com `.content`, `.stop_reason` e `.usage`."""
        ...


def _tipo(bloco: Any) -> str | None:
    return bloco.get("type") if isinstance(bloco, dict) else getattr(bloco, "type", None)


def _campo(bloco: Any, nome: str, padrao: Any = None) -> Any:
    return bloco.get(nome, padrao) if isinstance(bloco, dict) else getattr(bloco, nome, padrao)


# ---------------------------------------------------------------- local (regras)


class ProvedorLocal:
    """Motor por regras: gratuito, offline e determinístico."""

    nome = "local"

    def gerar(self, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]) -> Resposta:
        from app.motor_local import decidir

        return decidir(messages)


# ---------------------------------------------------------------- Ollama (LLM local gratuito)


class ProvedorOllama:
    """Fala com o Ollama pela API HTTP local (`/api/chat`), só com a biblioteca padrão."""

    nome = "ollama"

    def __init__(self, cfg: Config) -> None:
        self.url = cfg.ollama_url.rstrip("/") + "/api/chat"
        self.modelo = cfg.ollama_model
        self.timeout = cfg.timeout_api_s
        self.tentativas = cfg.max_tentativas_api

    @staticmethod
    def converter_mensagens(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Formato Messages API -> formato de chat do Ollama."""
        saida: list[dict[str, Any]] = [{"role": "system", "content": system}]
        nomes_por_id: dict[str, str] = {}
        for msg in messages:
            conteudo = msg["content"]
            if isinstance(conteudo, str):
                saida.append({"role": msg["role"], "content": conteudo})
                continue
            if msg["role"] == "assistant":
                textos, chamadas = [], []
                for b in conteudo:
                    if _tipo(b) == "text":
                        textos.append(_campo(b, "text", ""))
                    elif _tipo(b) == "tool_use":
                        nomes_por_id[_campo(b, "id")] = _campo(b, "name")
                        chamadas.append({"function": {"name": _campo(b, "name"), "arguments": _campo(b, "input", {})}})
                item: dict[str, Any] = {"role": "assistant", "content": "\n".join(textos)}
                if chamadas:
                    item["tool_calls"] = chamadas
                saida.append(item)
            else:
                for b in conteudo:
                    if _tipo(b) == "tool_result":
                        saida.append({
                            "role": "tool",
                            "tool_name": nomes_por_id.get(_campo(b, "tool_use_id"), ""),
                            "content": _campo(b, "content", ""),
                        })
                    elif _tipo(b) == "text":
                        saida.append({"role": "user", "content": _campo(b, "text", "")})
        return saida

    @staticmethod
    def converter_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {"type": "function", "function": {"name": t["name"], "description": t["description"],
                                              "parameters": t["input_schema"]}}
            for t in tools
        ]

    def gerar(self, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]) -> Resposta:
        corpo = json.dumps({
            "model": self.modelo,
            "messages": self.converter_mensagens(system, messages),
            "tools": self.converter_tools(tools),
            "stream": False,
            "options": {"temperature": 0.2},
        }).encode()
        dados = self._post(corpo)
        msg = dados.get("message", {})
        blocos: list[Bloco] = []
        if msg.get("content", "").strip():
            blocos.append(texto(msg["content"].strip()))
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            blocos.append(chamada(fn.get("name", ""), **args))
        uso = Uso(input_tokens=dados.get("prompt_eval_count", 0), output_tokens=dados.get("eval_count", 0))
        return resposta(*blocos, uso=uso)

    def _post(self, corpo: bytes) -> dict[str, Any]:
        req = urllib.request.Request(self.url, data=corpo, headers={"Content-Type": "application/json"})
        for tentativa in range(self.tentativas):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                detalhe = e.read().decode(errors="replace")[:300]
                if e.code < 500 or tentativa == self.tentativas - 1:
                    raise ErroProvedor(f"Ollama respondeu {e.code}: {detalhe}") from e
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if tentativa == self.tentativas - 1:
                    raise ErroProvedor(
                        f"Não consegui falar com o Ollama em {self.url}. Ele está rodando? ({e})"
                    ) from e
            espera = min(2**tentativa + random.uniform(0, 1), 30.0)
            logger.warning("Ollama: nova tentativa %d em %.1fs", tentativa + 2, espera)
            time.sleep(espera)
        raise ErroProvedor("inalcançável")


# ---------------------------------------------------------------- Anthropic (opcional, pago)


class ProvedorAnthropic:
    """Claude via Messages API. Requer o pacote `anthropic` e ANTHROPIC_API_KEY."""

    nome = "anthropic"

    def __init__(self, cfg: Config, client: Any | None = None) -> None:
        import anthropic  # import tardio: o pacote só é necessário neste provedor

        self._anthropic = anthropic
        self.cfg = cfg
        # max_retries=0: o retry com backoff é feito aqui, com log.
        self.client = client or anthropic.Anthropic(
            api_key=cfg.anthropic_api_key, max_retries=0, timeout=cfg.timeout_api_s
        )

    def gerar(self, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]) -> Any:
        a = self._anthropic
        params: dict[str, Any] = {
            "model": self.cfg.model,
            "max_tokens": self.cfg.max_tokens,
            "system": system,
            "tools": tools,
            "messages": messages,
            "output_config": {"effort": self.cfg.effort},
            "cache_control": {"type": "ephemeral"},
        }
        for tentativa in range(self.cfg.max_tentativas_api):
            try:
                if self.cfg.usar_fallback:
                    return self.client.beta.messages.create(
                        **params, betas=["server-side-fallback-2026-07-01"], fallbacks="default"
                    )
                return self.client.messages.create(**params)
            except (a.RateLimitError, a.APIConnectionError, a.InternalServerError) as e:
                if tentativa == self.cfg.max_tentativas_api - 1:
                    raise ErroProvedor(str(e)) from e
                espera = self._tempo_espera(e, tentativa)
                logger.warning("API: %s, nova tentativa %d em %.1fs", type(e).__name__, tentativa + 2, espera)
                time.sleep(espera)
            except a.APIError as e:
                raise ErroProvedor(str(e)) from e
        raise ErroProvedor("inalcançável")

    @staticmethod
    def _tempo_espera(erro: Exception, tentativa: int) -> float:
        resp = getattr(erro, "response", None)
        if resp is not None:
            retry_after = resp.headers.get("retry-after")
            if retry_after and retry_after.replace(".", "", 1).isdigit():
                return min(float(retry_after), 60.0)
        return min(2**tentativa + random.uniform(0, 1), 30.0)


# ---------------------------------------------------------------- fábrica


def criar_provedor(cfg: Config) -> Provedor:
    """Escolhe o provedor pela variável PROVEDOR (local | ollama | anthropic)."""
    nome = cfg.provedor.lower()
    if nome == "local":
        return ProvedorLocal()
    if nome == "ollama":
        return ProvedorOllama(cfg)
    if nome == "anthropic":
        return ProvedorAnthropic(cfg)
    raise ValueError(f"PROVEDOR inválido: {cfg.provedor!r}. Use local, ollama ou anthropic.")
