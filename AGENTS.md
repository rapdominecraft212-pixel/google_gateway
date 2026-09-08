# AGENTS.md — Regras deste repositório

## REGRA 1 — PROIBIDO MOCK EM TESTE. TERMINANTEMENTE. SEM EXCEÇÃO.

Nenhum teste neste projeto pode usar mock, stub, fake, patch de função,
servidor falso (`mock_gemini.py`), `unittest.mock`, `side_effect`, ou qualquer
substituto de componente real. **Literalmente nunca.**

Todos os testes devem exercitar o **código real de produção** contra o
**Google real** (ou recursos reais locais: banco SQLite real, rede real,
processo real). Se um teste não consegue rodar sem fingir um componente, isso
é um **sintoma de acoplamento** a ser corrigido no código — não um motivo para
criar um mock.

### Por quê (incidente real, 2026-09-06)
O `_nova_conexao` quebrava o parse de host quando a base tinha caminho
(`https://host/v1beta/openai` → `getaddrinfo("host/v1beta/openai")` → 11001).
Os 88 testes passavam porque o mock usava base **sem caminho**
(`http://127.0.0.1:porta`). Produção ficou **12 horas** com o chat morto
enquanto o poller (código real, sem mock) funcionava. O mock não divergiu da
realidade por acaso — diverge sempre que a realidade muda. Mock é uma aposta
de que você sabe o que o sistema real faz; essa aposta perde.

### O que é permitido (é teste real)
- **Testes unitários de funções de produção com entradas reais** (ex.:
  `estimar_tokens`, `bucket_id`, `next_wave_ts`, `_host_port`) — sem patch,
  sem fake: chamam a função real com dados reais e conferem a saída real.
- **Testes de integração contra o Google real** usando as chaves reais do
  `config.json` — connect real, TLS real, resposta real do Google.
- **Testes contra recursos reais locais** — SQLite real em disco, processo
  uvicorn real, porta real.

### O que é proibido (é mock)
- Qualquer servidor falso de API (o antigo `mock_gemini.py` foi apagado por esta regra).
- `unittest.mock` / `patch` / `side_effect` em qualquer teste.
- Fakes de `abrir_chat`, de respostas HTTP, de `usage`, de relógio
  (`time.sleep` real é permitido; relógio fake não).
- Qualquer "simulação" que substitua um componente de produção.

### Disciplina de custo (testes reais sem queimar cota)
Teste real não é sinônimo de teste caro. Ordem de preferência:
1. `/v1beta/models` (GET) — **não consome cota de generateContent**; serve
   para chave, rede, DNS, TLS, parse de resposta.
2. `generateContent` com `maxOutputTokens: 1` e prompt de 1 palavra — gasta
   ~1-2 tokens; serve para o caminho completo do chat.
3. Payloads grandes (100k+) — **somente** em teste de integração explícito
   de benchmark, nunca na suíte de regressão.
- Toda suíte real deve reportar o custo estimado em tokens no fim.
- Nunca paralelizar testes reais além da capacidade real do pool.

### Estado atual
A dívida foi paga: `mock_gemini.py` e toda suíte construída sobre ele foram
apagados. O que restou em `teste/` exercita código real (funções puras de
pacing/estado + SQLite real em disco); os caminhos de rede são validados
contra o Google real por `teste/harness_producao.py` e
`teste/benchmark.py --yes-real`. Novos testes seguem o ciclo
criar → testar intensivamente → apagar descrito em `teste/README.md`.

## REGRA 2 — TODO TESTE VIVE NA PASTA `teste/`. CENTRALIZAÇÃO TOTAL.

Todo e qualquer código que verifique, teste ou meça o sistema — teste unitário,
de integração, de carga, benchmark, harness, simulação, ou um script de "será
que está funcionando?" — **deve ser criado dentro de `teste/`**. É **proibido**
criar, na raiz ou em `app/`, qualquer arquivo, função ou endpoint cujo
propósito seja testar/verificar o sistema. Se é teste, é aqui.

No rastreador de trabalho (Redmine/Jira), nenhum item que produza código de
teste pode apontar para fora de `teste/`: todo entregável de verificação nasce
e morre nessa pasta.

Objetivo: centralização — um único lugar para olhar quando se quer saber como
provamos que o sistema funciona. O ciclo de vida (criar baseado no código real
→ testar intensivamente → apagar) e as regras detalhadas estão em
`teste/README.md`.

## REGRA 3 — NÃO CRIAR TESTES DESNECESSÁRIOS. PRIORIZAR DIAGNÓSTICO E LEITURA DE CÓDIGO.

Não perca tempo criando baterias de testes quando a análise de código e logs
já entrega a resposta. Testar demora, queima tempo e cota. O foco do agente
deve ser:

1. **Diagnosticar primeiro**: leia o código, entenda o fluxo ponta a ponta e
   localize a causa raiz examinando as funções de produção e os logs reais.
2. **Só teste se estritamente indispensável**: se você já compreende o sistema
   e o problema, resolva ou diagnostique diretamente no código.
3. **Velocidade e resultado**: tempo é prioridade. Menos testes exploratórios,
   mais profundidade de análise e ação direta.
