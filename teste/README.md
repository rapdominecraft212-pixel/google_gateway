# `teste/` — a ÚNICA pasta de testes do sistema

Tudo que serve para **verificar, testar ou medir a capacidade** deste sistema
vive **aqui dentro**. Não existe outro lugar. Centralizar para ninguém se
perder: um único endereço para responder "como provamos que o sistema
funciona?".

---

## REGRA DE CENTRALIZAÇÃO (inviolável)

- Qualquer código cujo propósito seja **testar / verificar / medir** o sistema
  — teste unitário, de integração, de carga, benchmark, harness, simulação de
  tarefa, ou um script de "será que está funcionando do jeito X?" — **deve ser
  criado dentro de `teste/`**.
- É **proibido** criar, na raiz do repositório ou dentro de `app/`, qualquer
  arquivo, função, endpoint ou comando que teste/verifique o sistema. Se é
  teste, é aqui. Sem exceção.
- A mesma proibição vale para o rastreador de trabalho (Redmine/Jira): nenhum
  item que produza código de teste pode apontar para fora desta pasta. Todo
  entregável de verificação nasce e morre em `teste/`.

---

## COMO UM TESTE DEVE SER FEITO (REGRA 1 do `AGENTS.md`)

**PROIBIDO MOCK. SEM EXCEÇÃO.** Um teste exercita o **código real de produção**
contra o **Google real** ou contra **recursos reais locais** (SQLite real em
disco, processo uvicorn real, porta real, rede real).

Nada de: `unittest.mock`, `patch`, `side_effect`, `MagicMock`, servidor falso
de API, fake de resposta / `usage` / relógio, ou qualquer "simulação" que
substitua um componente de produção. Se um teste não roda sem fingir algo, o
**acoplamento** é que está errado — corrija o código, não crie um mock.

> Motivo (incidente real): o mock usava uma base sem caminho e escondia um bug
> de parse que derrubou o chat em produção por 12 horas enquanto a suíte
> "88/88 verde" sorria. Mock é uma aposta de que você sabe o que o sistema real
> faz; essa aposta sempre perde.

Três camadas, da mais barata para a mais cara:

1. **Unidade real** — função de produção chamada com dados reais (ex.:
   `bucket_id`, `estimar_tokens`, `_host_port`). Zero rede, zero cota.
2. **Integração real barata** — `GET /v1beta/models` (não consome cota de
   `generateContent`) e `generateContent` com `maxOutputTokens: 1`. Valida
   chave, DNS, TLS, parse e o caminho completo do chat gastando ~1-2 tokens.
3. **Integração real de carga** — payloads grandes (100k+), **somente** em
   benchmark explícito, nunca na verificação do dia a dia.

Disciplina de custo: toda suíte real **reporta o total de tokens** no fim;
nunca paralelize além da capacidade real do pool.

---

## CICLO DE VIDA DE UM TESTE: criar → testar intensivamente → apagar

Um teste aqui é uma **prova temporária**, não um arquivo eterno. O fluxo
obrigatório é:

1. **CRIAR** — escreva o teste **baseado no código real** (nunca contra a sua
   suposição do código). Ele deve falhar se o comportamento real regredir.
2. **TESTAR INTENSIVAMENTE** — é a fase de martelar: rode o teste muitas
   vezes, sob carga, em condições reais, até ter certeza de que ele prova o
   que precisa provar e de que o sistema passa (ou falha honestamente). Um
   teste que nunca foi apanhado em execução não vale nada.
3. **APAGAR** — cumprido o objetivo (comportamento provado e/ou correção
   validada), **remova o teste da pasta**. O que permanece no repositório é a
   produção corrigida; a prova foi feita e se vai. Manter teste morto
   enganando quem vem depois é pior do que não ter teste.

**Exceção durável:** os *harnesses e ferramentas de medição reutilizáveis*
(`harness_producao.py`, `benchmark.py`, `simular_tarefa.py`) ficam na pasta
porque são instrumentos usados repetidamente, não provas de um único
comportamento.

---

## O QUE HÁ NESTA PASTA (estado atual)

| Arquivo | Tipo | Como roda |
|---|---|---|
| `teste_onda.py` | unidade real (pacing/estado) | suíte abaixo |
| `teste_honesto_429.py` | unidade real + SQLite real | suíte abaixo |
| `teste_erros_mudos.py` | unidade real (descrição de erro, assinaturas em SQLite, repasse tipado, aprendizado de 404) | suíte abaixo |
| `harness_producao.py` | integração real (Google real) | servidor no ar |
| `harness_opencode_gateway.py` | carga real multi-agente pelo caminho exato do opencode (streaming, 3.8-flash); prova "zero erro visível ao usuário" absorvendo tempestade de capacidade | `--yes-real --agentes N --turnos T` |
| `teste_cota_real.py` | medição real de cota: limite por chave, independência entre projetos, throttle por modelo | `--yes-real` |
| `ler_trilha.py` | leitor da trilha do dia (só leitura local, zero cota): reconstrói pedido a pedido + veredito do porquê do rate limit | `--desde HH:MM --ate HH:MM` |
| `terminal_interno.py` | terminal interno AO VIVO: segue a trilha em tempo real, envia mensagem real assistindo a maquinaria (chaves, falhas, cooldowns, esperas, racers) e faz replay do log | `--msg "..."` / `--duracao N` / `--ultimos N` / `--req ID` / `--desde HH:MM` |
| `benchmark.py` | carga real (Google real, consome cota) | `--yes-real` |
| `simular_tarefa.py` | tarefa real multi-etapa (gateway no ar) | servidor no ar |
| `analise_kpis.py` | medidor de KPIs (SQLite real; prova das correções) | `python teste\analise_kpis.py` |

---

## COMO RODAR

```bat
:: suíte de unidade real (sem cota, sem rede)
python -m unittest discover -s teste -t .

:: conversa real contra o gateway (Google real)
python teste\harness_producao.py

:: benchmark de carga real (confirma consumo de cota)
python teste\benchmark.py --yes-real --turns 10

:: simulação de tarefa de agente contra o gateway no ar
python teste\simular_tarefa.py
```
