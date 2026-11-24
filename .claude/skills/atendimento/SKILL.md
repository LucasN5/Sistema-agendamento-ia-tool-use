---
name: atendimento
description: Conversa com a Lia, agente de atendimento da loja, enviando a mensagem do usuário para o script do projeto e mostrando a resposta. Use só quando o usuário digitar /atendimento.
argument-hint: "<mensagem> | novo"
disable-model-invocation: true
allowed-tools: Bash(./agente-atendimento/atendimento:*)
---

Você é só o canal entre o usuário e a Lia. Não responda no lugar dela e não chame as ferramentas dela por conta própria.

Argumentos recebidos: `$ARGUMENTS`

1. Sem argumentos: rode `./agente-atendimento/atendimento --novo --sessao claude-code` para começar uma conversa nova. Depois mostre: "Lia: Olá! Sou a Lia, assistente da loja. Como posso ajudar?". Diga também que cada mensagem vai com `/atendimento <mensagem>` e que `/atendimento novo` reinicia a conversa.
2. Argumento `novo`: faça o mesmo que no passo 1.
3. Qualquer outro texto: é a mensagem do cliente. Envie pelo stdin, para que aspas e acentos cheguem intactos:

   ```bash
   ./agente-atendimento/atendimento - --sessao claude-code --json <<'MENSAGEM_LIA'
   $ARGUMENTS
   MENSAGEM_LIA
   ```

   Rode a partir da raiz do repositório. Mostre `Lia: <resposta>` exatamente como veio no campo `resposta`, sem resumir nem reescrever. Se o campo `tools` não estiver vazio, acrescente uma linha discreta: `(ferramentas: nome1, nome2)`.

Por padrão a Lia usa o motor local gratuito (`PROVEDOR=local`), que não precisa de chave nem de internet. Se o comando falhar, mostre o erro em uma frase. Se a Lia responder "instabilidade", o provedor configurado em `agente-atendimento/.env` não está acessível: o Ollama pode estar parado, ou falta `ANTHROPIC_API_KEY`. Sugira voltar para `PROVEDOR=local`.
