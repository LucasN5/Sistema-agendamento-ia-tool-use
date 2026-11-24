# AGENTS.md

Guia para agentes de código (Claude Code, Codex etc.) que trabalham neste repositório.

## O que é o projeto

Repositório **Sistema-atendimento-ia-tool-use**: a **Lia**, um agente de atendimento ao cliente de uma loja online, em português
do Brasil, construído com **tool use**. A Lia responde sobre pedidos, entregas, trocas e políticas (troca,
frete, reembolso) chamando funções Python, em vez de inventar informações.

Todo o código fica em `agente-atendimento/`. A documentação detalhada para humanos está em
`README.md` (na raiz).

## Stack

- Python >= 3.10, `pydantic` v2, `fastapi` + `uvicorn`, `python-dotenv`, `pytest`.
- `anthropic` só é importado quando `PROVEDOR=anthropic` (import tardio em `app/provedores.py`).
- Sem banco de dados: os dados são mocks em memória (`app/db.py`).

## Provedores de "modelo" (variável `PROVEDOR`)

O cérebro do agente é trocável. Todos implementam `gerar(system, tools, messages)` e devolvem o formato da
Messages API (blocos `text`/`tool_use`, `stop_reason`, `usage`), então loop, tools e guardrails não mudam.

| `PROVEDOR` | Custo | Observação |
|---|---|---|
| `local` (padrão) | Grátis, offline | Motor por regras/palavras-chave em `app/motor_local.py`. Determinístico. |
| `ollama` | Grátis | LLM local via HTTP (`OLLAMA_URL`, `OLLAMA_MODEL=qwen2.5:3b`), só biblioteca padrão. |
| `anthropic` | Pago | Exige `ANTHROPIC_API_KEY`. Modelo padrão `claude-sonnet-5-5`, `effort`, cache e fallback beta de servidor. |

Configuração em `agente-atendimento/.env` (modelo em `.env.example`; `.env` é ignorado pelo git).

## Mapa do código (`agente-atendimento/`)

| Arquivo | Papel |
|---|---|
| `app/config.py` | `Config` (dataclass congelada) lida do ambiente; instância global `config`. |
| `app/provedores.py` | `ProvedorLocal`, `ProvedorOllama`, `ProvedorAnthropic`, `criar_provedor()`, retry com backoff. |
| `app/motor_local.py` | `classificar()` (intenções por palavras-chave) e fluxos `_fluxo_*` que chamam tools e formatam a resposta. |
| `app/db.py` | Dados mockados e funções de acesso. Único arquivo a trocar por banco/API real. |
| `app/tools.py` | Funções das tools, modelos Pydantic `*Args`, schemas `TOOLS` (`strict`), `REGISTRO`, `ContextoSessao`, `executar_tool`. |
| `app/prompts.py` | `SYSTEM_PROMPT` da Lia (usado por Ollama e Anthropic). |
| `app/agent.py` | `AgenteAtendimento`: loop agêntico, corte de histórico, logs JSON, exportar/restaurar estado. |
| `app/api.py` | FastAPI: `POST /chat` (`sessao_id`, `mensagem`) e `GET /health`; sessões em dicionário em memória. |
| `cli.py` / `atendimento` | Comando de terminal: chat interativo ou modo script; sessões salvas em `.sessoes/<sessao>.json`. |
| `tests/` | Unitários de tools, provedores e CLI; 6 cenários ponta a ponta do agente. |

Tools disponíveis: `buscar_cliente`, `consultar_pedido`, `rastrear_entrega`, `buscar_politica`,
`abrir_solicitacao_troca` (escrita) e `transferir_para_humano`.

Na raiz há também `.claude/skills/atendimento/SKILL.md`: o comando `/atendimento` do Claude Code, que só repassa
a mensagem para `./agente-atendimento/atendimento --sessao claude-code --json` e mostra a resposta da Lia sem
alterá-la.

## Comandos

Rode a partir de `agente-atendimento/`:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # ou: pip install -e .  (instala o comando `atendimento`)

pytest                                   # suíte gratuita (motor local); deve passar sempre
pytest -m live                           # cenários com Ollama/Anthropic reais (pulados sem serviço/chave)

./atendimento                            # chat interativo (/novo, /sair)
./atendimento "mensagem" --sessao x --json
uvicorn app.api:app --reload             # API em localhost:8000
LOG_LEVEL=INFO ./atendimento             # logs por turno no stderr
```

Estado verificado em 2026-11-24: `pytest` → 69 passed, 12 skipped (os `live`).

Dados de teste: `ana@exemplo.com` (pedidos 1001 entregue, 1002 em trânsito) e `bruno@exemplo.com`
(2001 processando, 2002 entregue fora do prazo de troca). Os testes fixam a data em 2026-11-24
(`tests/conftest.py`) e zeram o estado do mock a cada teste.

## Regras que não podem ser quebradas

Os guardrails vivem **no código**, não só no prompt. Ao mexer em tools ou no loop, preserve:

- **Erros como dados:** tools devolvem `{"erro": ...}`; `executar_tool` nunca levanta exceção.
- **Permissões dentro das funções:** pedido inexistente e pedido de outro cliente recebem a mesma mensagem
  (não vazar existência de pedidos).
- **`cliente_id` vem da sessão** (`ContextoSessao`), nunca do modelo: o dispatcher sobrescreve o valor.
  Com `cliente_id_autenticado`, `buscar_cliente` só aceita o e-mail desse cliente.
- **Escrita exige confirmação:** a 1ª chamada de uma tool com `escrita=True` só cria `acao_pendente` e devolve
  `confirmacao_necessaria`; a execução só acontece se a próxima mensagem do cliente for uma confirmação
  explícita (`eh_confirmacao`). Qualquer outra resposta cancela.
- **Validação Pydantic** (`extra="forbid"`) + `strict: true` e `additionalProperties: false` nos schemas.
- O loop respeita `MAX_ITERACOES` (estourou → transfere para humano) e `MAX_MENSAGENS_HISTORICO` (corta num
  turno do cliente e remove blocos `thinking`).
- Só a resposta vai para o stdout do CLI; logs vão para o stderr (o `/atendimento` depende disso).

## Como adicionar uma nova tool

1. Função de acesso em `app/db.py`.
2. Função da tool em `app/tools.py`, validando permissões e devolvendo `{"erro": ...}` nas falhas.
3. Classe `XxxArgs(_Args)` com os campos e restrições.
4. Schema em `TOOLS`, com descrição dizendo **quando** usar; `enum` para valores fechados.
5. Entrada em `REGISTRO` (`usa_cliente=True` se usa o cliente da sessão, `escrita=True` se grava, `validar=` para
   checar elegibilidade antes de pedir confirmação).
6. Regra de negócio nova → `app/prompts.py`.
7. Motor local: intenção em `classificar()` e um `_fluxo_<nome>()` em `app/motor_local.py`
   (sem isso a tool não funciona com `PROVEDOR=local`).
8. Testes: unitário em `tests/test_tools.py` e cenário em `tests/test_agent_cenarios.py`.

## Convenções

- Código, nomes, mensagens, docstrings e commits em **português do Brasil**.
- `from __future__ import annotations` e type hints em todos os módulos.
- Respostas da Lia: cordiais, no máximo 4 frases, sem ids internos nem nomes de tools.
- Não versionar `.env`, `.venv/` nem `.sessoes/`.

## Próximos passos indicados para produção

- Trocar `app/db.py` por um repositório real mantendo as assinaturas.
- Guardar sessões da API em Redis (serializando `historico` e `contexto`, com TTL).
- Injetar `cliente_id_autenticado` a partir do login.
