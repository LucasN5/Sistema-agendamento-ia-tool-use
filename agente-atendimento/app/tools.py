"""Ferramentas do agente: funções, schemas JSON e dispatcher.

Regras de ouro:
- Erros voltam como dados (`{"erro": "..."}`), nunca como exceção.
- Permissões são validadas dentro das funções, não só no prompt.
- O dispatcher (`executar_tool`) aplica guardrails de sessão quando recebe um
  `ContextoSessao`: injeta o `cliente_id` da sessão e exige confirmação para
  ações de escrita.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app import db
from app.config import config

logger = logging.getLogger("agente.tools")

PRAZO_TROCA_DIAS = 30
Resultado = dict[str, Any]


def _hoje() -> date:
    """Data atual (isolada para facilitar testes)."""
    return date.today()


# ---------------------------------------------------------------- validação de argumentos


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class BuscarClienteArgs(_Args):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ConsultarPedidoArgs(_Args):
    pedido_id: str = Field(min_length=1, max_length=20)
    cliente_id: str = Field(min_length=1, max_length=20)


class RastrearEntregaArgs(_Args):
    codigo_rastreio: str = Field(min_length=5, max_length=30)


class BuscarPoliticaArgs(_Args):
    tema: Literal["troca", "frete", "reembolso"]


class AbrirTrocaArgs(_Args):
    pedido_id: str = Field(min_length=1, max_length=20)
    cliente_id: str = Field(min_length=1, max_length=20)
    motivo: str = Field(min_length=3, max_length=500)


class TransferirArgs(_Args):
    resumo: str = Field(min_length=3, max_length=1000)
    prioridade: Literal["normal", "alta"]


# ---------------------------------------------------------------- funções


def buscar_cliente(email: str) -> Resultado:
    """Identifica o cliente pelo e-mail."""
    cliente = db.obter_cliente_por_email(email)
    if not cliente:
        return {"erro": "Nenhum cliente encontrado com esse e-mail."}
    return {"cliente_id": cliente["id"], "nome": cliente["nome"]}


def _pedido_do_cliente(pedido_id: str, cliente_id: str) -> tuple[dict[str, Any] | None, Resultado | None]:
    """Busca o pedido e garante que pertence ao cliente. Retorna (pedido, erro)."""
    pedido = db.obter_pedido(pedido_id)
    # Mesma mensagem para "não existe" e "é de outro cliente": não vaza existência de pedidos alheios.
    if not pedido or pedido["cliente_id"] != cliente_id:
        return None, {"erro": "Pedido não encontrado para este cliente."}
    return pedido, None


def consultar_pedido(pedido_id: str, cliente_id: str) -> Resultado:
    """Status, itens, total e rastreio de um pedido do cliente."""
    pedido, erro = _pedido_do_cliente(pedido_id, cliente_id)
    if erro:
        return erro
    assert pedido is not None
    return {
        "pedido_id": pedido["id"],
        "status": pedido["status"],
        "data_compra": pedido["data_compra"],
        "data_entrega": pedido["data_entrega"],
        "itens": pedido["itens"],
        "total": pedido["total"],
        "codigo_rastreio": pedido["codigo_rastreio"],
    }


_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="transportadora")


def rastrear_entrega(codigo_rastreio: str) -> Resultado:
    """Consulta a transportadora (mock) com timeout."""
    futuro = _executor.submit(db.consultar_transportadora, codigo_rastreio)
    try:
        eventos = futuro.result(timeout=config.timeout_transportadora_s)
    except FuturesTimeout:
        return {"erro": "A transportadora não respondeu a tempo. Tente novamente em instantes."}
    if eventos is None:
        return {"erro": "Código de rastreio não encontrado na transportadora."}
    return {"codigo_rastreio": codigo_rastreio, "eventos": eventos, "ultimo_evento": eventos[-1] if eventos else None}


def buscar_politica(tema: str) -> Resultado:
    """Texto oficial da política da loja."""
    texto = db.obter_politica(tema)
    if texto is None:
        return {"erro": f"Tema inválido: {tema}. Use troca, frete ou reembolso."}
    return {"tema": tema, "politica": texto}


def validar_troca(pedido_id: str, cliente_id: str) -> Resultado | None:
    """Checa elegibilidade da troca. Retorna um erro ou None se puder abrir."""
    pedido, erro = _pedido_do_cliente(pedido_id, cliente_id)
    if erro:
        return erro
    assert pedido is not None
    if pedido["status"] != "entregue":
        return {"erro": f"Troca só é possível para pedidos entregues. Status atual: {pedido['status']}."}
    dias = (_hoje() - date.fromisoformat(pedido["data_entrega"])).days
    if dias > PRAZO_TROCA_DIAS:
        return {"erro": f"Prazo de troca expirado: entregue há {dias} dias (limite {PRAZO_TROCA_DIAS})."}
    existente = db.troca_existente(pedido["id"])
    if existente:
        return {"erro": f"Já existe uma troca aberta para este pedido: {existente['protocolo']}."}
    return None


def abrir_solicitacao_troca(pedido_id: str, cliente_id: str, motivo: str) -> Resultado:
    """Abre a troca (escrita) se o pedido for do cliente e estiver entregue."""
    erro = validar_troca(pedido_id, cliente_id)
    if erro:
        return erro
    registro = db.registrar_solicitacao_troca(pedido_id.strip().lstrip("#"), cliente_id, motivo)
    return {"protocolo": registro["protocolo"], "status": registro["status"], "pedido_id": registro["pedido_id"]}


def transferir_para_humano(resumo: str, prioridade: str, cliente_id: str | None = None) -> Resultado:
    """Coloca o atendimento na fila de atendentes humanos."""
    registro = db.registrar_transferencia(resumo, prioridade, cliente_id)
    return {"protocolo": registro["protocolo"], "status": registro["status"], "prioridade": prioridade}


# ---------------------------------------------------------------- schemas (o que o modelo vê)

TOOLS: list[dict[str, Any]] = [
    {
        "name": "buscar_cliente",
        "description": (
            "Identifica o cliente a partir do e-mail cadastrado. Use SEMPRE antes de consultar pedidos, "
            "rastrear um pedido ou abrir troca. Se o cliente ainda não informou o e-mail, peça-o "
            "em vez de chamar esta ferramenta. Retorna cliente_id e nome."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"email": {"type": "string", "description": "E-mail informado pelo cliente."}},
            "required": ["email"],
            "additionalProperties": False,
        },
    },
    {
        "name": "consultar_pedido",
        "description": (
            "Consulta status, itens, total, data de entrega e código de rastreio de um pedido. Use quando o "
            "cliente perguntar sobre um pedido específico, depois de identificá-lo com buscar_cliente. "
            "Só retorna pedidos que pertencem ao cliente identificado."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pedido_id": {"type": "string", "description": "Número do pedido, ex.: 1001."},
                "cliente_id": {"type": "string", "description": "cliente_id retornado por buscar_cliente."},
            },
            "required": ["pedido_id", "cliente_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "rastrear_entrega",
        "description": (
            "Consulta na transportadora o histórico de eventos de uma entrega. Use quando o cliente quiser "
            "saber onde está o pacote. Obtenha o código de rastreio com consultar_pedido; nunca use um "
            "código que não veio dessa consulta."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"codigo_rastreio": {"type": "string", "description": "Código, ex.: BR123456789."}},
            "required": ["codigo_rastreio"],
            "additionalProperties": False,
        },
    },
    {
        "name": "buscar_politica",
        "description": (
            "Retorna o texto oficial de uma política da loja. Use sempre que o cliente perguntar sobre "
            "regras, prazos ou condições de troca, frete ou reembolso. Nunca responda essas perguntas de memória."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"tema": {"type": "string", "enum": ["troca", "frete", "reembolso"]}},
            "required": ["tema"],
            "additionalProperties": False,
        },
    },
    {
        "name": "abrir_solicitacao_troca",
        "description": (
            "Abre uma solicitação de troca e retorna um protocolo TR-XXXXXX. AÇÃO DE ESCRITA: use somente "
            "depois de resumir a ação ao cliente e receber confirmação explícita (ex.: 'sim, pode abrir'). "
            "Só funciona para pedidos entregues, do próprio cliente e dentro do prazo. Se o retorno for "
            "status 'confirmacao_necessaria', peça a confirmação e não diga que a troca foi aberta."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pedido_id": {"type": "string"},
                "cliente_id": {"type": "string", "description": "cliente_id retornado por buscar_cliente."},
                "motivo": {"type": "string", "description": "Motivo da troca nas palavras do cliente."},
            },
            "required": ["pedido_id", "cliente_id", "motivo"],
            "additionalProperties": False,
        },
    },
    {
        "name": "transferir_para_humano",
        "description": (
            "Transfere o atendimento para um atendente humano. Use quando o cliente pedir para falar com uma "
            "pessoa, demonstrar forte insatisfação, ou quando o assunto estiver fora do escopo (pedidos, "
            "entregas, trocas e políticas). Use prioridade 'alta' para cliente muito insatisfeito ou "
            "problema urgente."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "resumo": {"type": "string", "description": (
                    "Resumo do caso para o atendente, em 1 a 3 frases: motivo da transferência, cliente, "
                    "pedido envolvido e o que já foi tentado e com qual resultado (ex.: troca recusada por prazo)."
                )},
                "prioridade": {"type": "string", "enum": ["normal", "alta"]},
            },
            "required": ["resumo", "prioridade"],
            "additionalProperties": False,
        },
    },
]
# strict: garante que o input do modelo siga o schema exatamente.
TOOLS = [{**t, "strict": True} for t in TOOLS]


# ---------------------------------------------------------------- registro e dispatcher


@dataclass(frozen=True)
class _Registro:
    funcao: Callable[..., Resultado]
    args: type[_Args]
    usa_cliente: bool = False  # cliente_id vem da sessão
    escrita: bool = False  # exige confirmação
    validar: Callable[..., Resultado | None] | None = None  # pré-checagem antes de pedir confirmação


REGISTRO: dict[str, _Registro] = {
    "buscar_cliente": _Registro(buscar_cliente, BuscarClienteArgs),
    "consultar_pedido": _Registro(consultar_pedido, ConsultarPedidoArgs, usa_cliente=True),
    "rastrear_entrega": _Registro(rastrear_entrega, RastrearEntregaArgs),
    "buscar_politica": _Registro(buscar_politica, BuscarPoliticaArgs),
    "abrir_solicitacao_troca": _Registro(
        abrir_solicitacao_troca, AbrirTrocaArgs, usa_cliente=True, escrita=True,
        validar=lambda pedido_id, cliente_id, **_: validar_troca(pedido_id, cliente_id),
    ),
    "transferir_para_humano": _Registro(transferir_para_humano, TransferirArgs),
}


@dataclass
class ContextoSessao:
    """Estado de segurança da sessão, controlado pelo código (não pelo modelo).

    - `cliente_id_autenticado`: em produção vem do login; quando definido, o modelo
      não consegue consultar outro cliente.
    - `cliente_id`: cliente efetivo da conversa (autenticado ou identificado por e-mail).
    - `acao_pendente`: ação de escrita aguardando confirmação do cliente.
    """

    cliente_id_autenticado: str | None = None
    cliente_id: str | None = None
    cliente_nome: str | None = None
    acao_pendente: dict[str, Any] | None = None
    acao_confirmada: bool = False

    def __post_init__(self) -> None:
        if self.cliente_id_autenticado and not self.cliente_id:
            self.cliente_id = self.cliente_id_autenticado


_AFIRMATIVO = re.compile(
    r"^\s*(sim|s|confirmo|confirmado|confirma|pode|ok|okay|isso|correto|certo|claro|positivo|fechado|perfeito)\b",
    re.IGNORECASE,
)
_NEGATIVO = re.compile(r"\b(n[ãa]o|cancela|cancelar|espera|pera|aguarde|desisto)\b", re.IGNORECASE)


def eh_confirmacao(mensagem: str) -> bool:
    """Confirmação explícita do cliente ("sim", "pode abrir", "confirmo"...)."""
    return bool(_AFIRMATIVO.search(mensagem)) and not _NEGATIVO.search(mensagem)


def eh_negacao(mensagem: str) -> bool:
    return bool(_NEGATIVO.search(mensagem))


def _json(dados: Resultado) -> str:
    return json.dumps(dados, ensure_ascii=False)


def _aplicar_guardrails(nome: str, reg: _Registro, args: dict[str, Any], ctx: ContextoSessao) -> Resultado | None:
    """Injeta cliente_id e controla confirmação. Retorna um resultado para curto-circuitar, ou None."""
    if nome == "buscar_cliente" and ctx.cliente_id_autenticado:
        cliente = db.obter_cliente_por_email(args["email"])
        if not cliente or cliente["id"] != ctx.cliente_id_autenticado:
            return {"erro": "Este e-mail não corresponde ao cliente autenticado na sessão."}

    if reg.usa_cliente:
        if not ctx.cliente_id:
            return {"erro": "Cliente não identificado. Peça o e-mail e use buscar_cliente primeiro."}
        if args.get("cliente_id") != ctx.cliente_id:
            logger.warning("cliente_id do modelo (%s) substituído pelo da sessão (%s)", args.get("cliente_id"), ctx.cliente_id)
        args["cliente_id"] = ctx.cliente_id

    if nome == "transferir_para_humano":
        args["cliente_id"] = ctx.cliente_id

    if reg.escrita:
        chave = {"nome": nome, "pedido_id": str(args.get("pedido_id", "")).lstrip("#")}
        pendente = ctx.acao_pendente
        if pendente and ctx.acao_confirmada and {k: pendente[k] for k in chave} == chave:
            ctx.acao_pendente, ctx.acao_confirmada = None, False
            return None  # confirmado: executa
        erro = reg.validar(**args) if reg.validar else None
        if erro:
            return erro
        ctx.acao_pendente, ctx.acao_confirmada = {**chave, "argumentos": dict(args)}, False
        return {
            "status": "confirmacao_necessaria",
            "mensagem": (
                "Nada foi registrado ainda. Resuma a ação ao cliente (pedido e motivo) e peça confirmação "
                "explícita. Depois que ele confirmar, chame esta ferramenta novamente."
            ),
        }
    return None


def executar_tool(nome: str, argumentos: dict[str, Any], contexto: ContextoSessao | None = None) -> str:
    """Executa uma tool pelo nome e devolve JSON. Nunca levanta exceção.

    Sem `contexto`, executa a função diretamente (útil em testes e scripts).
    Com `contexto`, aplica os guardrails de sessão.
    """
    try:
        reg = REGISTRO.get(nome)
        if reg is None:
            return _json({"erro": f"Ferramenta inexistente: {nome}"})
        try:
            args = reg.args.model_validate(argumentos or {}).model_dump()
        except ValidationError as e:
            detalhes = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
            return _json({"erro": f"Argumentos inválidos: {detalhes}"})

        if contexto is not None:
            curto = _aplicar_guardrails(nome, reg, args, contexto)
            if curto is not None:
                return _json(curto)

        resultado = reg.funcao(**args)

        if contexto is not None and nome == "buscar_cliente" and "cliente_id" in resultado:
            contexto.cliente_id, contexto.cliente_nome = resultado["cliente_id"], resultado["nome"]
        return _json(resultado)
    except Exception as e:  # noqa: BLE001 - nenhuma falha de tool pode derrubar o agente
        logger.exception("Falha ao executar tool %s", nome)
        return _json({"erro": f"Falha interna ao executar {nome}: {type(e).__name__}"})
