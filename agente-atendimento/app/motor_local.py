"""Motor local por regras: decide quais tools chamar e formata a resposta, sem custo, sem chave e sem internet.

Ele recebe o histórico (mensagens com blocos `text`, `tool_use` e `tool_result`) e devolve a próxima resposta:
uma ou mais chamadas de tool, ou o texto final. Como o motor só decide e formata,
toda informação continua vindo das tools, e os guardrails do dispatcher valem igual.

Limitações: entende só os assuntos do escopo (pedido, rastreio, troca, políticas,
atendente humano) por palavras-chave.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from app.provedores import Resposta, _campo, _tipo, chamada, resposta, texto
from app.tools import eh_confirmacao, eh_negacao

# ---------------------------------------------------------------- linguagem


def normalizar(t: str) -> str:
    """Minúsculas e sem acentos, para comparar palavras-chave."""
    t = unicodedata.normalize("NFKD", t.lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def _tem(n: str, *termos: str) -> bool:
    return any(re.search(rf"\b{t}", n) for t in termos)


RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
RE_PEDIDO = re.compile(r"(?:pedido|compra|n[º°o]\.?|numero|#)\s*(?:de\s+)?(?:n[º°o]\.?\s*)?#?\s*(\d{3,8})\b", re.I)
RE_NUMERO = re.compile(r"(?<![\w@.])#?(\d{4,8})(?![\w@])")
RE_NOTA = re.compile(r"^\[Nota do sistema:[^\]]*\]\s*")
RE_NOTA_CLIENTE = re.compile(r"cliente_id (\S+?), nome ([^)]+)\)")

INSATISFEITO = ("absurd", "pessim", "horrivel", "ridicul", "palhacada", "procon", "reclame aqui", "revoltad",
                "inaceitavel", "vergonha", "nunca mais compro", "lixo", "descaso")
HUMANO = ("atendente", "humano", "pessoa real", "falar com alguem", "gerente", "supervisor", "operador")
FORA_ESCOPO = ("cancel", "cupom", "desconto", "nota fiscal", "boleto", "pagamento", "senha", "endereco",
               "parcel", "garantia", "cartao")
SAUDACOES = ("oi", "ola", "bom dia", "boa tarde", "boa noite", "e ai", "hey", "opa")
PERGUNTA = ("como", "qual", "quais", "quanto", "prazo", "regra", "politica", "posso", "funciona", "existe", "tem")


@dataclass
class Intencao:
    tipo: str  # pedido_status | rastrear | troca | politica | humano | fora_escopo | saudacao | ...
    temas: list[str] = field(default_factory=list)
    prioridade: str = "normal"


def classificar(msg: str) -> Intencao | None:
    """Intenção de uma mensagem do cliente, ou None se ela só traz dados (e-mail, número)."""
    n = normalizar(msg)
    tem_pedido = bool(RE_PEDIDO.search(msg) or RE_NUMERO.search(RE_EMAIL.sub("", msg)))
    pergunta = _tem(n, *PERGUNTA) and not _tem(n, "quero", "preciso", "gostaria", "solicit", "abr")

    if _tem(n, *INSATISFEITO):
        return Intencao("humano", prioridade="alta")
    if _tem(n, *HUMANO):
        return Intencao("humano")

    temas = [t for t, chaves in {
        "troca": ("troc",),
        "frete": ("frete", "prazo de entrega", "entrega gratis"),
        "reembolso": ("reembols", "estorn", "dinheiro de volta", "devolu", "devolv"),
    }.items() if _tem(n, *chaves)]

    if "troca" in temas and not (pergunta and not tem_pedido):
        return Intencao("troca")
    if _tem(n, "onde esta", "rastre", "cade", "chegou", "chega", "a caminho", "transportadora") or (
        _tem(n, "entrega") and tem_pedido and "frete" not in temas
    ):
        return Intencao("rastrear")
    if temas:
        return Intencao("politica", temas=temas)
    if _tem(n, *FORA_ESCOPO):
        return Intencao("fora_escopo")
    if _tem(n, "status", "situacao", "pedido", "compra", "andamento"):
        return Intencao("pedido_status")
    if _tem(n, "obrigad", "valeu", "agradec"):
        return Intencao("agradecimento")
    if RE_EMAIL.search(msg) or tem_pedido:
        return None
    if eh_negacao(msg) and len(n.split()) <= 6:
        return Intencao("encerrar")
    if any(re.fullmatch(rf"{s}\W*", n.strip()) or n.startswith(s + " ") or n.startswith(s + ",") for s in SAUDACOES):
        return Intencao("saudacao")
    return Intencao("desconhecida")


def extrair_pedido(msg: str) -> str | None:
    m = RE_PEDIDO.search(msg) or RE_NUMERO.search(RE_EMAIL.sub("", msg))
    return m.group(1) if m else None


def extrair_motivo(msg: str) -> str:
    m = re.search(r"(?:porque|pois|motivo:?|ja que|já que)\s+(.{3,200})", msg, re.I)
    if m:
        return m.group(1).strip(" .!")
    m = re.search(r"((?:ficou|veio|está|esta|chegou)\s+[\w\s]{2,60})", msg, re.I)
    if m:
        return m.group(1).strip(" .!").capitalize()
    n = normalizar(msg)
    for chave, motivo in (("defeit", "Produto com defeito"), ("quebrad", "Produto quebrado"),
                          ("errad", "Produto errado"), ("pequen", "Tamanho pequeno"), ("grand", "Tamanho grande")):
        if chave in n:
            return motivo
    return "Troca solicitada pelo cliente"


# ---------------------------------------------------------------- leitura do histórico


@dataclass
class Evento:
    indice: int  # posição da mensagem tool_result no histórico
    nome: str
    entrada: dict[str, Any]
    resultado: dict[str, Any]


@dataclass
class Conversa:
    textos_cliente: list[tuple[int, str]]  # (índice, texto)
    eventos: list[Evento]
    cliente_nota: dict[str, Any] | None = None  # cliente citado na nota de histórico cortado

    @property
    def inicio_turno(self) -> int:
        return self.textos_cliente[-1][0] if self.textos_cliente else 0

    @property
    def atual(self) -> str:
        return self.textos_cliente[-1][1] if self.textos_cliente else ""

    def do_turno(self, nome: str) -> Evento | None:
        evs = [e for e in self.eventos if e.indice > self.inicio_turno and e.nome == nome]
        return evs[-1] if evs else None

    def cliente(self) -> dict[str, Any] | None:
        for e in reversed(self.eventos):
            if e.nome == "buscar_cliente" and "cliente_id" in e.resultado:
                return e.resultado
        return self.cliente_nota

    def email_cliente(self) -> str | None:
        """E-mail com que o cliente da sessão foi identificado (None se ainda não foi ou se veio da nota)."""
        for e in reversed(self.eventos):
            if e.nome == "buscar_cliente" and "cliente_id" in e.resultado:
                return str(e.entrada.get("email", "")).strip().lower() or None
        return None

    def troca_pendente(self) -> dict[str, Any] | None:
        """Entrada da última troca que ficou aguardando confirmação antes deste turno."""
        anteriores = [e for e in self.eventos if e.nome == "abrir_solicitacao_troca" and e.indice < self.inicio_turno]
        if anteriores and anteriores[-1].resultado.get("status") == "confirmacao_necessaria":
            return anteriores[-1].entrada
        return None


def ler_conversa(messages: list[dict[str, Any]]) -> Conversa:
    textos: list[tuple[int, str]] = []
    chamadas: dict[str, tuple[str, dict[str, Any]]] = {}
    eventos: list[Evento] = []
    cliente_nota = None
    for i, msg in enumerate(messages):
        c = msg["content"]
        if msg["role"] == "user" and isinstance(c, str):
            if (nota := RE_NOTA.match(c)) and (m := RE_NOTA_CLIENTE.search(nota.group(0))):
                cliente_nota = {"cliente_id": m.group(1), "nome": m.group(2)}
            textos.append((i, RE_NOTA.sub("", c)))
            continue
        if not isinstance(c, list):
            continue
        for b in c:
            if _tipo(b) == "tool_use":
                chamadas[_campo(b, "id")] = (_campo(b, "name"), dict(_campo(b, "input", {}) or {}))
            elif _tipo(b) == "tool_result" and _campo(b, "tool_use_id") in chamadas:
                nome, entrada = chamadas[_campo(b, "tool_use_id")]
                try:
                    resultado = json.loads(_campo(b, "content", "{}"))
                except (TypeError, json.JSONDecodeError):
                    resultado = {"erro": "resultado ilegível"}
                eventos.append(Evento(i, nome, entrada, resultado))
    return Conversa(textos, eventos, cliente_nota)


# ---------------------------------------------------------------- formatação

STATUS = {"entregue": "entregue", "em_transito": "em trânsito", "processando": "em processamento"}


def _data(iso: str | None) -> str:
    if not iso:
        return ""
    a, m, d = iso[:10].split("-")
    return f"{d}/{m}/{a}{iso[10:]}"


def _reais(v: float) -> str:
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _itens(pedido: dict[str, Any]) -> str:
    nomes = [i["produto"] for i in pedido.get("itens", [])]
    return nomes[0] if len(nomes) == 1 else ", ".join(nomes[:-1]) + " e " + nomes[-1]


def _status(pedido: dict[str, Any]) -> str:
    s = STATUS.get(pedido["status"], pedido["status"])
    frase = f"Seu pedido {pedido['pedido_id']} ({_itens(pedido)}, total {_reais(pedido['total'])}) está {s}"
    if pedido["status"] == "entregue" and pedido.get("data_entrega"):
        frase = frase.replace(f"está {s}", f"foi entregue em {_data(pedido['data_entrega'])}")
    return frase + "."


def _erro(resultado: dict[str, Any]) -> str:
    return resultado.get("erro", "Não consegui concluir essa consulta.")


# ---------------------------------------------------------------- decisão


def decidir(messages: list[dict[str, Any]]) -> Resposta:
    """Próximo passo do agente: chamar tools ou responder."""
    conv = ler_conversa(messages)
    msg = conv.atual

    # Resposta a uma troca que aguarda confirmação.
    pendente = conv.troca_pendente()
    if pendente and not conv.do_turno("abrir_solicitacao_troca"):
        if eh_confirmacao(msg):
            return resposta(chamada("abrir_solicitacao_troca", **pendente))
        if eh_negacao(msg):
            return resposta(texto("Tudo bem, não abri a troca. Posso ajudar em algo mais?"))
    if pendente and conv.do_turno("abrir_solicitacao_troca"):
        return _fluxo_troca(conv, pendente.get("pedido_id"), pendente.get("motivo", ""), confirmando=True)

    # Troca de conta no meio da conversa: não busca o novo e-mail (nem confirma se ele existe).
    if troca := _troca_de_conta(conv):
        return troca

    intencao = classificar(msg)
    if intencao is None:  # só dados (e-mail ou número): continua o assunto anterior
        anteriores = [classificar(t) for _, t in conv.textos_cliente[-4:-1]]
        validas = [i for i in anteriores
                   if i and i.tipo not in {"saudacao", "agradecimento", "desconhecida", "encerrar"}]
        intencao = validas[-1] if validas else Intencao("identificar")

    if intencao.tipo == "humano":
        return _fluxo_humano(conv, intencao.prioridade, _motivo_transferencia(conv, intencao.prioridade))
    if intencao.tipo == "fora_escopo":
        return _fluxo_humano(conv, "normal", "Assunto fora do escopo do atendimento automático.")
    if intencao.tipo == "politica":
        return _fluxo_politica(conv, intencao.temas)
    if intencao.tipo == "saudacao":
        nome = (conv.cliente() or {}).get("nome", "")
        sauda = f"Olá, {nome.split()[0]}!" if nome else "Olá! Sou a Lia."
        return resposta(texto(f"{sauda} Posso consultar pedidos, rastrear entregas, abrir trocas e explicar as "
                              "políticas de troca, frete e reembolso. Como posso ajudar?"))
    if intencao.tipo == "encerrar":
        return resposta(texto("Tudo bem! Se precisar de mais alguma coisa, é só chamar."))
    if intencao.tipo == "agradecimento":
        return resposta(texto("Por nada! Se precisar de mais alguma coisa, é só chamar."))
    if intencao.tipo == "desconhecida":
        return resposta(texto("Desculpe, não entendi. Posso consultar pedidos, rastrear entregas, abrir trocas e "
                              "explicar as políticas de troca, frete e reembolso. Pode reformular? Se preferir, "
                              "peço para um atendente falar com você."))

    # Daqui em diante o assunto envolve o cliente: identificação primeiro.
    cliente, passo = _identificar(conv, intencao.tipo)
    if passo:
        return passo
    assert cliente is not None
    if intencao.tipo == "identificar":
        return resposta(texto(f"Obrigada, {cliente['nome'].split()[0]}! Como posso ajudar com seu pedido?"))

    pedido_id = _pedido_mencionado(conv)
    if not pedido_id:
        return resposta(texto(f"Certo, {cliente['nome'].split()[0]}! Qual é o número do pedido?"))

    if intencao.tipo == "troca":
        motivo = next((extrair_motivo(t) for _, t in reversed(conv.textos_cliente[-3:]) if "troc" in normalizar(t)),
                      extrair_motivo(msg))
        return _fluxo_troca(conv, pedido_id, motivo, confirmando=False, cliente_id=cliente["cliente_id"])
    return _fluxo_pedido(conv, pedido_id, cliente["cliente_id"], rastrear=intencao.tipo == "rastrear")


def _troca_de_conta(conv: Conversa) -> Resposta | None:
    """Recusa outro e-mail depois que a sessão já está vinculada a um cliente."""
    cliente, atual = conv.cliente(), conv.email_cliente()
    if not cliente or not atual or conv.do_turno("buscar_cliente"):
        return None
    novos = {e.lower() for e in RE_EMAIL.findall(conv.atual)} - {atual}
    if not novos:
        return None
    nome = cliente["nome"].split()[0]
    return resposta(texto(f"Este atendimento já está vinculado ao cadastro de {nome}, então não consigo trocar "
                          "de conta no meio da conversa. Se o pedido é de outra conta, o titular dela precisa "
                          f"iniciar um novo atendimento com o próprio e-mail. Posso ajudar com algo do cadastro "
                          f"de {nome}?"))


def _identificar(conv: Conversa, tipo: str) -> tuple[dict[str, Any] | None, Resposta | None]:
    cliente = conv.cliente()
    if cliente:
        return cliente, None
    busca = conv.do_turno("buscar_cliente")
    if busca and "erro" in busca.resultado:
        return None, resposta(texto(f"Não encontrei cadastro com o e-mail {busca.entrada.get('email')}. "
                                    "Pode conferir e me enviar de novo?"))
    email = next((m.group(0) for _, t in reversed(conv.textos_cliente[-4:]) if (m := RE_EMAIL.search(t))), None)
    if email:
        return None, resposta(chamada("buscar_cliente", email=email))
    if tipo == "identificar":
        return None, resposta(texto("Para eu localizar seu cadastro, qual é o e-mail da sua conta?"))
    return None, resposta(texto("Claro! Para consultar seu pedido, preciso do e-mail cadastrado na sua conta. "
                                "Qual é?"))


def _pedido_mencionado(conv: Conversa) -> str | None:
    for _, t in reversed(conv.textos_cliente[-4:]):
        if p := extrair_pedido(t):
            return p
    return None


def _fluxo_pedido(conv: Conversa, pedido_id: str, cliente_id: str, rastrear: bool) -> Resposta:
    consulta = conv.do_turno("consultar_pedido")
    if not consulta:
        return resposta(chamada("consultar_pedido", pedido_id=pedido_id, cliente_id=cliente_id))
    if "erro" in consulta.resultado:
        return resposta(texto(f"Não localizei o pedido {pedido_id} no seu cadastro. Pode conferir o número?"))
    pedido = consulta.resultado
    if not rastrear:
        return resposta(texto(_status(pedido)))
    codigo = pedido.get("codigo_rastreio")
    if not codigo:
        return resposta(texto(f"{_status(pedido)} Ele ainda não foi enviado, então não há código de rastreio."))
    rastreio = conv.do_turno("rastrear_entrega")
    if not rastreio:
        return resposta(chamada("rastrear_entrega", codigo_rastreio=codigo))
    if "erro" in rastreio.resultado:
        return resposta(texto(f"{_status(pedido)} {_erro(rastreio.resultado)}"))
    ultimo = rastreio.resultado["ultimo_evento"]
    return resposta(texto(f"{_status(pedido)} Última atualização da transportadora ({codigo}): "
                          f"{ultimo['evento']} em {ultimo['local']}, {_data(ultimo['data'])}."))


def _fluxo_troca(conv: Conversa, pedido_id: str | None, motivo: str, confirmando: bool,
                 cliente_id: str | None = None) -> Resposta:
    abrir = conv.do_turno("abrir_solicitacao_troca")
    if not confirmando:
        consulta = conv.do_turno("consultar_pedido")
        if not consulta:
            return resposta(chamada("consultar_pedido", pedido_id=pedido_id, cliente_id=cliente_id))
        if "erro" in consulta.resultado:
            return resposta(texto(f"Não localizei o pedido {pedido_id} no seu cadastro. Pode conferir o número?"))
        if not abrir:
            return resposta(chamada("abrir_solicitacao_troca", pedido_id=pedido_id, cliente_id=cliente_id,
                                    motivo=motivo))
    assert abrir is not None
    r = abrir.resultado
    if r.get("status") == "confirmacao_necessaria":
        consulta = conv.do_turno("consultar_pedido")
        itens = f" ({_itens(consulta.resultado)})" if consulta and "itens" in consulta.resultado else ""
        return resposta(texto(f"Vou abrir a troca do pedido {pedido_id}{itens} pelo motivo: "
                              f"\"{abrir.entrada.get('motivo')}\". Confirma? Responda \"sim\" para eu registrar."))
    if "protocolo" in r:
        return resposta(texto(f"Pronto! Sua troca do pedido {r['pedido_id']} foi registrada com o protocolo "
                              f"{r['protocolo']}. Guarde esse número para acompanhar a solicitação."))
    return resposta(texto(f"Não foi possível abrir a troca: {_erro(r)}"))


def _fluxo_politica(conv: Conversa, temas: list[str]) -> Resposta:
    obtidas = [e for e in conv.eventos if e.indice > conv.inicio_turno and e.nome == "buscar_politica"]
    if not obtidas:
        return resposta(*[chamada("buscar_politica", tema=t) for t in temas])  # chamadas em paralelo
    partes = [e.resultado["politica"] if len(obtidas) == 1 else f"{e.resultado['tema'].capitalize()}: "
              f"{e.resultado['politica']}" for e in obtidas if "politica" in e.resultado]
    return resposta(texto(" ".join(partes) or "Não consegui consultar a política agora."))


def _frase(t: str) -> str:
    return t.strip().rstrip(".") + "."


def _contexto_caso(conv: Conversa) -> list[str]:
    """O que já se sabe e já foi tentado na conversa, para o atendente não recomeçar do zero."""
    partes: list[str] = []
    if cliente := conv.cliente():
        partes.append(f"Cliente: {cliente.get('nome', '?')} ({cliente.get('cliente_id', '?')}).")
    fatos: dict[tuple[str, str], str] = {}  # última informação por (assunto, pedido/código)
    for e in conv.eventos:
        r, pid = e.resultado, str(e.entrada.get("pedido_id", "")).lstrip("#")
        if e.nome == "consultar_pedido":
            if "erro" in r:
                fatos[("pedido", pid)] = f"Pedido {pid} não localizado no cadastro do cliente."
            else:
                itens = f" ({_itens(r)})" if r.get("itens") else ""
                entrega = f" em {_data(r['data_entrega'])}" if r.get("data_entrega") else ""
                status = STATUS.get(r.get("status", ""), r.get("status", "?"))
                fatos[("pedido", pid)] = f"Pedido {pid}{itens}: {status}{entrega}."
        elif e.nome == "rastrear_entrega":
            codigo = e.entrada.get("codigo_rastreio", "")
            if ultimo := r.get("ultimo_evento"):
                fatos[("rastreio", codigo)] = (f"Rastreio {codigo}: {ultimo['evento']} em {ultimo['local']}, "
                                               f"{_data(ultimo['data'])}.")
            elif "erro" in r:
                fatos[("rastreio", codigo)] = f"Rastreio {codigo} falhou: {_frase(r['erro'])}"
        elif e.nome == "abrir_solicitacao_troca":
            motivo = e.entrada.get("motivo", "")
            if "protocolo" in r:
                fatos[("troca", pid)] = f"Troca do pedido {pid} aberta (protocolo {r['protocolo']})."
            elif r.get("status") == "confirmacao_necessaria":
                fatos[("troca", pid)] = f"Troca do pedido {pid} aguardando confirmação (motivo: {motivo})."
            else:
                fatos[("troca", pid)] = f"Troca do pedido {pid} recusada: {_frase(_erro(r))}"
    return partes + list(fatos.values())


def _motivo_transferencia(conv: Conversa, prioridade: str) -> str:
    """Primeira frase do resumo: por que o atendimento foi transferido."""
    if prioridade != "alta":
        return "Cliente pediu para falar com um atendente."
    motivo = "Cliente demonstrou forte insatisfação; atendimento prioritário."
    recentes = [normalizar(t) for _, t in conv.textos_cliente[-4:]]
    if any(_tem(n, *HUMANO) for n in recentes):
        motivo += " Também pediu para falar com um atendente."
    return motivo


def _fluxo_humano(conv: Conversa, prioridade: str, motivo: str) -> Resposta:
    transf = conv.do_turno("transferir_para_humano")
    if not transf:
        resumo = " ".join([motivo, *_contexto_caso(conv), f"Última mensagem: \"{conv.atual[:200]}\""])
        if len(resumo) > 1000:  # limite do schema da tool
            resumo = resumo[:997] + "..."
        return resposta(chamada("transferir_para_humano", resumo=resumo, prioridade=prioridade))
    protocolo = transf.resultado.get("protocolo", "")
    inicio = "Sinto muito pela experiência. " if prioridade == "alta" else ""
    return resposta(texto(f"{inicio}Transferi seu atendimento para um de nossos atendentes (protocolo {protocolo}). "
                          "Em breve alguém continua a conversa com você."))
