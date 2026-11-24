# Lia — agente de atendimento com tool use

Agente de atendimento ao cliente para uma loja online. Ele responde em português do Brasil e usa
*tool use*: a Lia resolve dúvidas sobre pedidos, entregas, trocas e políticas da loja chamando funções
Python, em vez de inventar informações.

Todo o código fica na pasta [`agente-atendimento/`](agente-atendimento/). Os comandos e caminhos abaixo são
relativos a ela, exceto o `/atendimento` do Claude Code, que roda na raiz do repositório.

**Funciona de graça, sem chave de API e sem internet.** O "cérebro" do agente é um motor local por regras
(`app/motor_local.py`): ele entende os assuntos do escopo por palavras-chave, decide quais tools chamar e
escreve a resposta a partir dos resultados. É determinístico e instantâneo.

## Setup

```bash
cd agente-atendimento
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # opcional: só para mudar limites e timeouts
```

## Como rodar

Os comandos desta seção rodam dentro de `agente-atendimento/`, com o ambiente virtual ativo.

**Terminal: comando `atendimento`**

```bash
pip install -e .                   # instala o comando `atendimento` (ou use ./atendimento sem instalar)

atendimento                        # chat interativo (/novo reinicia, /sair encerra)
atendimento --sessao maria         # chat que grava a conversa e pode ser retomado depois
LOG_LEVEL=INFO atendimento         # mostra no stderr os logs por turno (tools, iterações, latência)
```

**Modo script** (uma mensagem por execução; a conversa fica salva em `.sessoes/<sessao>.json`):

```bash
atendimento "Sou ana@exemplo.com e quero trocar o pedido 1001, ficou pequeno" --sessao maria
atendimento "Sim, pode abrir" --sessao maria          # continua a mesma conversa
echo "Cadê meu pedido?" | atendimento - --sessao maria  # mensagem pelo stdin
atendimento "oi" --sessao maria --json                # {"sessao", "resposta", "tools": [...]}
atendimento --novo --sessao maria                     # apaga a conversa
```

Só a resposta vai para o stdout (os logs vão para o stderr), então dá para usar o comando em pipes e
scripts. As solicitações mock (trocas e transferências) também ficam salvas em `.sessoes/`, assim os
protocolos continuam a numeração entre execuções.

**No Claude Code: `/atendimento`**

Com o Claude Code aberto na raiz do repositório:

```
/atendimento                                   # começa uma conversa nova
/atendimento Sou ana@exemplo.com, cadê o pedido 1002?
/atendimento novo                              # reinicia
```

O comando fica em `.claude/skills/atendimento/SKILL.md`. Ele chama `./agente-atendimento/atendimento` na
sessão `claude-code` e mostra a resposta da Lia sem alterações.

**API:**

```bash
uvicorn app.api:app --reload
curl -X POST localhost:8000/chat -H 'Content-Type: application/json' \
  -d '{"sessao_id": "abc", "mensagem": "Meu e-mail é ana@exemplo.com, cadê o pedido 1002?"}'
# {"resposta": "..."}
```

**Testes:**

```bash
pytest              # unitários, cenários roteirizados e os 6 cenários com o motor local
```

Dados mockados para testar: `ana@exemplo.com` (pedidos 1001 entregue e 1002 em trânsito) e
`bruno@exemplo.com` (2001 processando e 2002 entregue fora do prazo de troca).

## Arquitetura

Caminhos relativos a `agente-atendimento/`.

| Arquivo | Papel |
|---|---|
| `app/config.py` | Lê o `.env` (limites do loop e timeouts). |
| `app/provedores.py` | Formato das respostas (`text`/`tool_use`), interface `gerar()` e `ProvedorLocal`. |
| `app/motor_local.py` | Motor por regras: decide as tools e escreve a resposta a partir dos resultados. |
| `app/db.py` | Dados mockados e **funções de acesso**. É o único arquivo a trocar por banco ou API real. |
| `app/tools.py` | Funções, schemas JSON (`strict`), modelos Pydantic, registro e o dispatcher `executar_tool`. |
| `app/agent.py` | `AgenteAtendimento`: loop agêntico, corte de histórico e logs. |
| `app/api.py` | FastAPI `POST /chat`, com um agente por sessão. |
| `cli.py` / `atendimento` | Comando de terminal: chat interativo e modo script com sessão salva em arquivo. |

### Loop agêntico

A cada iteração (até `MAX_ITERACOES`), o agente:

1. Chama o motor local com o histórico completo.
2. Adiciona ao histórico o conteúdo inteiro da resposta (blocos text e tool_use).
3. Se `stop_reason != "tool_use"`, devolve o texto.
4. Senão, executa **todas** as tools pedidas e devolve os `tool_result` numa única mensagem `user`.

Se o limite estourar, ele transfere para um atendente e pede desculpas.

### Guardrails (no código)

- **Permissões dentro das funções:** `consultar_pedido` e `abrir_solicitacao_troca` verificam se o pedido é
  do cliente. Pedido de outro cliente e pedido inexistente recebem a mesma resposta, para não revelar que o
  pedido existe.
- **`cliente_id` da sessão:** o dispatcher sobrescreve o `cliente_id` escolhido pelo modelo pelo cliente
  identificado na sessão (`ContextoSessao`). Em produção, crie o agente com
  `AgenteAtendimento(cliente_id_autenticado=<id do login>)`. Assim, `buscar_cliente` só aceita o e-mail desse
  cliente.
- **Confirmação de escrita:** na primeira chamada, `abrir_solicitacao_troca` só registra uma `acao_pendente` e
  devolve `confirmacao_necessaria`. A troca só é gravada se a mensagem seguinte do cliente for uma confirmação
  explícita ("sim", "pode", "confirmo"…). Qualquer outra resposta cancela a ação pendente.
- **Validação Pydantic** de todos os argumentos, além de `strict: true` nos schemas.
- **Erros como dados:** `{"erro": ...}`, enviados com `is_error: true`. Nenhuma exceção derruba o agente.
- **Histórico limitado:** acima de `MAX_MENSAGENS_HISTORICO`, o corte acontece num turno do cliente, sem
  separar um `tool_use` do seu `tool_result`.
- **Timeout** na consulta à transportadora (`TIMEOUT_TRANSPORTADORA_S`).
- **Logs estruturados** por turno, em JSON: tools chamadas com argumentos e latência, número de iterações e
  latência total.

## Como adicionar uma nova tool

Exemplo: `cancelar_pedido`.

1. **Dados** (`app/db.py`): crie a função de acesso, por exemplo `marcar_pedido_cancelado(pedido_id)`.
2. **Função** (`app/tools.py`): implemente `cancelar_pedido(pedido_id, cliente_id) -> dict`. Valide as
   permissões dentro dela (dono do pedido, status permitido) e devolva `{"erro": ...}` nas falhas.
3. **Argumentos:** crie `class CancelarPedidoArgs(_Args)` com os campos e as restrições.
4. **Schema:** adicione um item em `TOOLS` com uma descrição que diga **quando** usar a tool. Use `enum` para
   valores fechados e `additionalProperties: false`.
5. **Registro:** adicione a tool a `REGISTRO`, por exemplo
   `"cancelar_pedido": _Registro(cancelar_pedido, CancelarPedidoArgs, usa_cliente=True, escrita=True)`.
   - `usa_cliente=True`: o `cliente_id` vem da sessão.
   - `escrita=True`: exige confirmação.
   - `validar=`: função opcional que checa a elegibilidade antes de pedir confirmação ao cliente.
6. **Motor local:** em `app/motor_local.py`, adicione uma intenção em `classificar()` (palavras-chave) e um
   fluxo `_fluxo_<nome>()` que chama a tool e formata a resposta a partir do resultado.
7. **Testes:** um teste unitário em `tests/test_tools.py` e um cenário em `tests/test_agent_cenarios.py`.

## Para produção

- Troque `app/db.py` por um repositório real, mantendo as assinaturas.
- Guarde as sessões em Redis em vez do dicionário em memória de `api.py`, serializando `historico` e
  `contexto` e definindo um TTL.
- Injete `cliente_id_autenticado` a partir do login.
