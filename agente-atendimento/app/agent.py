"""Loop agêntico da Lia (tool use manual, com provedor de modelo trocável)."""

from __future__ import annotations

import dataclasses
import json
import logging
import time
from typing import Any

from app.config import Config, config
from app.prompts import SYSTEM_PROMPT
from app.provedores import ErroProvedor, Provedor, ProvedorAnthropic, criar_provedor
from app.tools import TOOLS, ContextoSessao, eh_confirmacao, executar_tool

logger = logging.getLogger("agente")

MSG_LIMITE_ITERACOES = (
    "Desculpe, não consegui concluir seu atendimento por aqui. "
    "Vou transferir você para um de nossos atendentes, que continuará de onde paramos."
)
MSG_RECUSA = "Desculpe, não posso ajudar com esse pedido por aqui. Posso transferir você para um atendente?"
MSG_ERRO_API = "Estamos com instabilidade no momento. Por favor, tente novamente em alguns instantes."


def _tipo(bloco: Any) -> str | None:
    """Tipo de um bloco de conteúdo, seja objeto do SDK ou dict (estado restaurado)."""
    return bloco.get("type") if isinstance(bloco, dict) else getattr(bloco, "type", None)


def _para_dict(bloco: Any) -> Any:
    """Converte um bloco do SDK em dict JSON, preservando os campos que a API exige de volta."""
    if isinstance(bloco, dict):
        return bloco
    if hasattr(bloco, "model_dump"):
        return bloco.model_dump(mode="json", exclude_none=True)
    return dict(vars(bloco))


class AgenteAtendimento:
    """Uma conversa com a Lia. Mantém histórico e estado de segurança da sessão."""

    def __init__(
        self,
        provedor: Provedor | None = None,
        cliente_id_autenticado: str | None = None,
        cfg: Config = config,
        sessao_id: str = "local",
        client: Any | None = None,
    ) -> None:
        """`provedor` padrão vem de PROVEDOR no .env (local, gratuito). `client` injeta um cliente
        Anthropic já criado (usado em testes)."""
        self.cfg = cfg
        self.sessao_id = sessao_id
        if provedor is None:
            provedor = ProvedorAnthropic(cfg, client) if client is not None else criar_provedor(cfg)
        self.provedor = provedor
        self.historico: list[dict[str, Any]] = []
        self.contexto = ContextoSessao(cliente_id_autenticado=cliente_id_autenticado)
        self.tools_chamadas: list[tuple[str, dict[str, Any]]] = []  # toda a conversa, em ordem

    # ------------------------------------------------------------ persistência

    def exportar_estado(self) -> dict[str, Any]:
        """Estado serializável em JSON (histórico + contexto de segurança), para arquivo ou Redis."""
        historico = []
        for msg in self.historico:
            conteudo = msg["content"]
            if isinstance(conteudo, list):
                conteudo = [_para_dict(b) for b in conteudo]
            historico.append({"role": msg["role"], "content": conteudo})
        return {
            "sessao_id": self.sessao_id,
            "historico": historico,
            "contexto": dataclasses.asdict(self.contexto),
            "tools_chamadas": [[n, a] for n, a in self.tools_chamadas],
        }

    @classmethod
    def restaurar(cls, estado: dict[str, Any], provedor: Provedor | None = None, cfg: Config = config) -> AgenteAtendimento:
        """Recria um agente a partir de `exportar_estado()`."""
        agente = cls(provedor=provedor, cfg=cfg, sessao_id=estado.get("sessao_id", "local"))
        agente.historico = estado.get("historico", [])
        agente.contexto = ContextoSessao(**estado.get("contexto", {}))
        agente.tools_chamadas = [(n, a) for n, a in estado.get("tools_chamadas", [])]
        return agente

    # ------------------------------------------------------------ API pública

    def responder(self, mensagem: str) -> str:
        """Processa uma mensagem do cliente e devolve a resposta da Lia."""
        inicio = time.perf_counter()
        log_turno: dict[str, Any] = {"evento": "turno", "sessao": self.sessao_id, "provedor": self.provedor.nome,
                                     "tools": [], "iteracoes": 0,
                                     "tokens": {"entrada": 0, "saida": 0, "cache_leitura": 0}}
        self._registrar_confirmacao(mensagem)
        self.historico.append({"role": "user", "content": mensagem})
        self._limitar_historico()

        try:
            resposta_final = self._loop(log_turno)
        except ErroProvedor as e:
            logger.error("Falha do provedor após retries: %s", e)
            resposta_final = MSG_ERRO_API
            log_turno["erro"] = type(e).__name__
            # Mantém o histórico válido para o próximo turno (user/assistant alternados).
            self.historico.append({"role": "assistant", "content": resposta_final})

        log_turno["latencia_ms"] = round((time.perf_counter() - inicio) * 1000)
        logger.info(json.dumps(log_turno, ensure_ascii=False))
        return resposta_final

    # ------------------------------------------------------------ loop

    def _loop(self, log_turno: dict[str, Any]) -> str:
        for _ in range(self.cfg.max_iteracoes):
            log_turno["iteracoes"] += 1
            resposta = self.provedor.gerar(SYSTEM_PROMPT, TOOLS, self.historico)
            self._somar_uso(log_turno, resposta)

            # Guarda o conteúdo inteiro (thinking, text, tool_use): o modelo precisa dele na próxima chamada.
            self.historico.append({"role": "assistant", "content": resposta.content})

            if resposta.stop_reason == "refusal":
                return MSG_RECUSA
            if resposta.stop_reason != "tool_use":
                return self._texto(resposta.content)

            resultados = []
            for bloco in resposta.content:
                if _tipo(bloco) != "tool_use":
                    continue
                t0 = time.perf_counter()
                saida = executar_tool(bloco.name, dict(bloco.input), self.contexto)
                ms = round((time.perf_counter() - t0) * 1000)
                self.tools_chamadas.append((bloco.name, dict(bloco.input)))
                logger.info("tool=%s args=%s", bloco.name, json.dumps(bloco.input, ensure_ascii=False))
                log_turno["tools"].append({"nome": bloco.name, "args": bloco.input, "latencia_ms": ms})
                resultado: dict[str, Any] = {"type": "tool_result", "tool_use_id": bloco.id, "content": saida}
                if saida.startswith('{"erro"'):
                    resultado["is_error"] = True
                resultados.append(resultado)
            # Todos os resultados da rodada numa única mensagem de role user.
            self.historico.append({"role": "user", "content": resultados})

        log_turno["limite_iteracoes"] = True
        executar_tool(
            "transferir_para_humano",
            {"resumo": "Limite de iterações do agente atingido; cliente precisa de ajuda humana.", "prioridade": "normal"},
            self.contexto,
        )
        self.historico.append({"role": "assistant", "content": MSG_LIMITE_ITERACOES})
        return MSG_LIMITE_ITERACOES

    # ------------------------------------------------------------ guardrails

    def _registrar_confirmacao(self, mensagem: str) -> None:
        """Se há ação de escrita pendente, marca como confirmada só com um 'sim' explícito do cliente."""
        if not self.contexto.acao_pendente:
            return
        if eh_confirmacao(mensagem):
            self.contexto.acao_confirmada = True
        else:
            self.contexto.acao_pendente, self.contexto.acao_confirmada = None, False

    def _limitar_historico(self) -> None:
        """Corta mensagens antigas quando o histórico passa do limite.

        Corta sempre num turno de texto do cliente (nunca no meio de um tool_use/tool_result),
        reduz para metade do limite para não cortar a cada turno, e remove os blocos de
        thinking das mensagens mantidas: eles ficam presos à conversa original e não
        sobrevivem a uma edição do histórico. O estado de segurança (cliente identificado,
        ação pendente) fica no `contexto`, então não se perde.
        """
        limite = self.cfg.max_mensagens_historico
        if len(self.historico) <= limite:
            return
        alvo = len(self.historico) - limite // 2
        corte = next(
            (i for i in range(alvo, len(self.historico))
             if self.historico[i]["role"] == "user" and isinstance(self.historico[i]["content"], str)),
            len(self.historico) - 1,
        )
        mantidas = self.historico[corte:]
        for msg in mantidas:
            if msg["role"] == "assistant" and isinstance(msg["content"], list):
                msg["content"] = [b for b in msg["content"] if _tipo(b) != "thinking"]
        nota = "[Nota do sistema: mensagens antigas desta conversa foram removidas."
        if self.contexto.cliente_id:
            nota += (f" O cliente já foi identificado (cliente_id {self.contexto.cliente_id}, "
                     f"nome {self.contexto.cliente_nome or 'desconhecido'}).")
        primeira = mantidas[0]
        primeira["content"] = f"{nota}]\n\n{primeira['content']}"
        removidas = len(self.historico) - len(mantidas)
        self.historico = mantidas
        logger.info(json.dumps({"evento": "historico_cortado", "sessao": self.sessao_id, "removidas": removidas}))

    # ------------------------------------------------------------ utilitários

    @staticmethod
    def _texto(conteudo: list[Any]) -> str:
        return "\n".join(b.text for b in conteudo if _tipo(b) == "text").strip()

    @staticmethod
    def _somar_uso(log_turno: dict[str, Any], resposta: Any) -> None:
        uso = getattr(resposta, "usage", None)
        if uso is None:
            return
        log_turno["tokens"]["entrada"] += getattr(uso, "input_tokens", 0) or 0
        log_turno["tokens"]["saida"] += getattr(uso, "output_tokens", 0) or 0
        log_turno["tokens"]["cache_leitura"] += getattr(uso, "cache_read_input_tokens", 0) or 0
