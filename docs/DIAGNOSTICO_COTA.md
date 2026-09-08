# Diagnóstico: "com 36 chaves, como a cota acaba no meio do processo?"

> Data: 2026-09-08 · Método: forense do `logs/eventos-diagnostico.log` de produção
> (2.658 eventos reais de 2026-09-07) + teste ao vivo do gateway + leitura de código.
> Zero mock: todos os números abaixo vêm de tráfego real ou de execução real.

---

## TL;DR

**O gateway modela cota POR CHAVE. O Google cobra cota POR PROJETO.**
As 36 keys criadas no AI Studio pertencem (provavelmente) ao **mesmo projeto** —
então o pool inteiro compartilha **UM** balde de RPM/TPM/RPD. O gateway acha que
tem 36 × (20 RPM / 250k TPM) = 720 RPM / 9M TPM; a realidade é 20 RPM / 250k TPM
**no total**. Quando o contexto do opencode cresce (39k → 96k tokens/pedido),
3 pedidos grandes no mesmo minuto estouram o TPM do projeto — e **todas** as
chaves começam a dar 429 ao mesmo tempo, inclusive as "frias". O failover então
**queima mais cota do projeto em vez de ajudar**, o pedido pendura ~35–120s
esperando e morre com 429 `sem_chave`. É exatamente o sintoma relatado.

> ⚠️ **Ver seção 9 — INCIDENTE**: em 07/09 às 23:12 as 36 keys passaram a dar
> **401** (desativadas) cerca de 1h após o zip com elas em texto plano subir
> para um repositório **público** no GitHub. O gateway não está "instável"
> agora: está **sem nenhuma key viva**.

---

## 1. O sintoma (relato)

> "Ele lê 30 arquivos, me dá uma resposta normal. Depois eu peço outra coisa,
> ele lê, demora bastante e dá erro."

## 2. A evidência (log de produção, 2026-09-07)

| Métrica | Valor | O que significa |
|---|---|---|
| Pedidos no dia | 105 (89 ok, 16 `sem_chave`) | 15% dos pedidos morreram sem chave |
| Falhas por chave no caminho | **1.053** (1.029 × 429, 24 × 503) | quase 10 falhas por pedido, em média |
| 429 com janela local ZERADA | **987 de 1.029 (96%)** | `reqs_60s=0, tokens_60s=0`: chave fria e o Google ainda disse "cota excedida" ⇒ a cota estourada **não é da chave** |
| Distribuição das falhas | **todas** as 36 keys: ~30–31 falhas cada | esgotamento **uniforme** = assinatura de balde compartilhado |
| Pior pedido | `chaves_queimadas: 144` | 4 passadas completas pela pool (144 = 4 × 36) |
| Contexto por pedido | mediana 37.822 tokens, máx 96.005 | cada request reenvia a conversa inteira |
| Episódio crítico | 20:19–20:27 (11 `sem_chave`, ~660 falhas) | o "meio do processo" do relato |
| Latência dos `sem_chave` | 35s a 120s | o "demora bastante" = deadline de espera (`max_espera_requisicao_seg: 120`) |

### Timeline do colapso (tokens estimados/minuto vs falhas)

```
19:52   17 pedidos, ~840k tokens no minuto   → tudo OK (última hora boa)
19:53   3 pedidos, 177k tokens               → 96 falhas 429  (estourou)
19:54   0 pedidos novos                      → 122 falhas 429  (retry preso)
19:55   0 pedidos novos                      →  18 falhas + 1 sem_chave
20:02–20:09  volta intermitente (ok com falhas esparsas)
20:19–20:27  colapso total: ~660 falhas, 11 sem_chave, quase nada ok
22:05   volta a funcionar sozinho (janela de minuto do Google escoou)
```

O recovery em minutos **descarta cota diária (RPD)**: foi estouro de **minuto**
(RPM/TPM). 840k tokens/min ≫ 250k TPM — se o balde for do projeto, estourou 3×.

## 3. Como funciona esse bug (a mecânica, passo a passo)

1. **O contexto cresce sem parar.** O protocolo é stateless: cada pedido do
   opencode reenvia a conversa inteira. Na sessão do log, o pedido foi de
   39k → 96k tokens conforme o agente lia arquivos. Três pedidos de ~90k no
   mesmo minuto = ~270k tokens — acima do TPM 250k **de um único balde**.
2. **O Google cobra no projeto.** Doc oficial (RELATORIO_GEMINI_API.md §8.1):
   "Limites são **por projeto**, não por chave". O AI Studio cria **um**
   projeto e nele ficam todas as keys (§1). Cota do pool = cota de 1 key.
3. **O projeto estoura → todas as keys dão 429 juntas.** 96% dos 429 chegaram
   com a janela local da chave zerada — o gateway não tem como ver o contador
   do projeto; ele conta por chave (estado.py/janelas.py).
4. **O failover reage do jeito errado.** Foi desenhado para chaves
   independentes: 429 na Google-7 ⇒ "problema dela, tenta a Google-8". Sob
   cota compartilhada, cada tentativa **queima mais RPM do mesmo projeto** e
   mantém o balde estourado. Um pedido chegou a 144 tentativas.
5. **E ainda acelera.** O sinal dos racers (paralelismo) é alimentado por
   qualquer falha, inclusive quota (`proxy.py` → `registrar_sinal_racers` no
   `finally`): na tempestade o paralelismo subiu de 1 → 2 → 3 (cap 6). Mais
   pedidos simultâneos contra o mesmo balde exaurido = tempestade pior.
6. **O cliente espera e morre.** Enquanto isso o pedido cicla (espera 5s ×
   `max_ciclos_espera: 2`, deadline 120s) — o "demora bastante" — até o
   gateway devolver 429 `sem_chave` — o "dá erro".
7. **Reinicia o ciclo.** O opencode tenta de novo, os cooldowns de 15s por
   chave expiram, nova tempestade. Durou ~7 min no episódio das 20:19.

**Por que 36 chaves não ajudam:** se todas são do mesmo projeto, elas dão
redundância de *chave* (uma ser banida não derruba o pool), mas **zero**
redundância de *cota*. O pool É uma chave só, aos olhos do Google.

## 4. Mapa de bugs

| # | Severidade | Bug | Evidência | Onde no código |
|---|---|---|---|---|
| B1 | 🔴 crítica | Modelo de cota **por chave**; realidade é **por projeto**. Capacidade superestimada em ~36× (anunciada ao cliente!) | 96% dos 429 com janela zerada; queima uniforme; `pool_tpm: 9.000.000` no corpo do erro | `roteador.py`, `estado.py`, `janelas.py` (todo o modelo); `endpoints.py:86-87` (`pool_rpm = rpm × n`) |
| B2 | 🔴 crítica | Failover sem noção de balde comum: `tentativas_por_pedido` = **todas** as chaves; 1 pedido ⇒ até 36–144 requests ao projeto exaurido | `chaves_queimadas: 144` no log | `app/config.py:127-137` |
| B3 | 🟠 alta | Racers (paralelismo) alimentados por falha de **quota**: na tempestade de 429 o gateway ataca **mais forte** | racers base 1→2→3 (ma 2.7) durante as tempestades | `app/proxy.py` (`finally` → `registrar_sinal_racers`), `app/estado.py:242-273` |
| B4 | 🟠 alta | Limites do config nunca conferidos com o tier real (`rpm: 20, tpm: 250000, rpd: 1500` por chave) — se o free tier real for menor, tudo acima piora | a confirmar com P1 do `teste_cota_real.py` | `config.json` (`limites`) |
| B5 | 🟡 média | Crescimento de contexto do cliente sem freio: 39k → 96k tokens/pedido na mesma sessão (gatilho do estouro; não é defeito do gateway, mas ele pode mitigar) | coluna `tokens_est` do log | client-side (opencode: compact/subagents) |
| B6 | 🟡 média | `historico.db` ficou **readonly** 13× em produção ⇒ contabilidade/statísticas perdidas nesses períodos (cega o diagnóstico, não causa o 429) | 13 eventos `db_readonly_causa` | `app/db.py` (a investigar: lock de antivírus/arquivo no Windows) |

## 5. Testes reais executados

### 5.1 Forense do log de produção (evidência §2)
Parser completo dos 2.658 eventos: contagens, timeline, distribuição por chave,
visão local vs resposta do Google, escalada de racers. Zero teoria — tudo medido.

### 5.2 Gateway ao vivo, cenário de falha total (neste ambiente)
Servidor real (`servidor.py`, uvicorn + SQLite reais, config real de 36 chaves),
com `GEMINI_API_BASE` apontando para endereço com conexão recusada (erro de rede
real e instantâneo, sem mock):

```
POST /v1/chat/completions {"model":"gemini-3.8-flash","messages":[{"role":"user","content":"oi"}]}
→ HTTP 429 em 45.0s
  corpo: "rede indisponivel ... aguardando a rede"
  cota:  {"chaves_ativas":36, "pool_rpm":720, "pool_tpm":9000000}
  trilha: 9 chaves tentadas serialmente (Google-1 → Google-9), 45s de espera, sem_chave
```

Reproduz a mecânica do sintoma (espera longa + 429) e **prova o anúncio de
capacidade 36×** (B1) no próprio corpo de erro.

### 5.3 Validação remota de keys (inconclusiva por formato, não por invalidez)
Pela rede da plataforma: key falsa genérica → `400 API_KEY_INVALID`; key falsa
no formato novo `AQ.Ab8…` → `401 ACCESS_TOKEN_TYPE_UNSUPPORTED`; suas keys →
o mesmo 401 da falsa-formato-novo ⇒ o 401 é **roteamento por formato** do canal
GET sem header, **não** indica keys mortas. Validação de verdade precisa do
header `Authorization: Bearer` — feito abaixo, na sua máquina.

## 6. O que rodar na SUA máquina (decisivo, ~500 tokens de custo)

### Jeito fácil — GitHub Desktop no Windows (fluxo de 2 cliques)

1. Aceite este PR no GitHub Desktop e abra a pasta local do repositório.
2. **Dois cliques em `RODAR-TESTE-COTA.bat`** (na raiz do repo). Ele sozinho:
   - acha o Python (`python` → `py -3` → caminho padrão do instalador python.org);
   - se não houver `config.json` no clone (ele fica fora do git de propósito),
     copia do Monitor-Google original (`Desktop\Monitor-Google`) ou pede o caminho;
   - roda o teste real capturando **tudo** em `teste/resultados/cota-real-*.txt`:
     ambiente (máquina/Python/branch/commit), config sanitizado (keys ocultas),
     saída completa do teste e custo no fim.
3. No GitHub Desktop: vai aparecer 1 arquivo novo em `teste\resultados\` —
   escreva o commit e clique em **Push origin**.
4. O resultado chega ao repositório e a análise/correção (F1–F6) continua.

Validar a canalização sem gastar cota (opcional):
`python teste\coletar_dados_cota.py --ensaio`
(o teste recusa rodar sem `--yes-real` e sai — isso é o esperado).

Interpretando: o arquivo de resultados traz tudo; os campos-chave são o
**P2** (chave B fria recusa junto com A saturada ⇒ balde compartilhado) e o
**P1** (RPM real medido por chave ⇒ limites corretos do tier).

### Jeito manual — linha de comando

O repo já tem o teste exato para isto (`teste/teste_cota_real.py`, zero mock,
usa a função de produção `gemini_api.abrir_chat`):

```
python teste/teste_cota_real.py --yes-real
```

- **P0** radiografia das 36 keys (todas vivas?)
- **P1** quantas reqs/min UMA key aguenta antes do 429 (=RPM real do tier ⇒ B4)
- **P2** saturar a key A **contamina** a key B fria? ← **se sim, B1 confirmado**
- **P3** a key saturada ainda serve outro modelo? (throttle por modelo)
- **P4** ritmo sustentável da pool

**Leitura do P2:** se logo após saturar a key A a key B (fria, sem uso) também
tomar 429, o balde é compartilhado — B1 vira fato, e a correção F1 abaixo é
obrigatória. Se B continuar OK, o problema é outro (então B4/B2 levam a culpa).

## 7. Plano de correção (ordem de prioridade)

1. **F1 — cota por grupo/projeto**: declarar no config quais keys compartilham
   projeto (`"projeto": "..."` por chave, ou modo global
   `"cota_compartilhada": true`). Janelas de RPM/TPM passam a ser somadas **por
   grupo**; roteador escolhe grupo com folga; admissão preditiva de TPM
   (que já existe por chave em `roteador.py`) passa a valer pelo grupo.
2. **F2 — failover consciente de quota**: 429-quota em ≥N keys do mesmo grupo
   em <X s ⇒ cooldown do **grupo inteiro** (com `retry_after` do Google quando
   houver), em vez de queimar as 36. Corta a tempestade na raiz (B2).
3. **F3 — racers só para capacidade**: falha tipo `quota` não alimenta
   `registrar_sinal_racers` (só 503/rede alimentam) (B3).
4. **F4 — conferir limites reais**: AI Studio → Rate limits; ajustar
   `config.json` com o que o P1 medir (B4).
5. **F5 — mitigação de contexto**: compactação/subagents no opencode para
   conter 39k→96k (B5); no gateway, orçamento de TPM por grupo já ajuda.
6. **F6 — db readonly**: reproduzir/caçar o lock no Windows (B6).

## 8. Perguntas em aberto

- As 36 keys são todas do mesmo projeto? (P2 responde; AI Studio → Projects
  também mostra a coluna de projeto de cada key.)
- O tier é Free mesmo? Qual o RPM/TPM/RPD real do `gemini-3.8-flash` nele?
  (AI Studio → Rate limits; o 429 do P1 traz o corpo completo do Google.)
- O `sem_chave` instantâneo (2ms) às 18:48 logo após restart merece inspeção
  (estado de cooldown sobrevive ao restart?).

---

## 9. INCIDENTE 2026-09-07 23:12 — as 36 keys morreram juntas (401)

Resultado real do `RODAR-TESTE-COTA.bat` (`teste/resultados/cota-real-20260907-231229.txt`):

```
P0: 36/36 chaves → HTTP 401 (resposta em 1–2,5s; zero 200, zero 429)
```

O caminho testado é o de produção (`gemini_api.abrir_chat`: POST no endpoint
OpenAI-compat com `Authorization: Bearer` + `x-goog-api-key`) — o mesmo que
funcionava horas antes.

### Timeline (horário local de Brasília, UTC-3)

| Hora | Evento |
|---|---|
| 22:08–22:13 | últimos pedidos 200 OK nos logs de produção |
| **22:19** | repositório `google_gateway` criado no GitHub como **PÚBLICO**, commit inicial com o `Monitor-Google.zip` (**36 keys em texto plano**) |
| ~22:45 | validação remota: keys reais já davam 401 (na época lido como artefato de canal) |
| **23:12** | teste real: **TODAS as 36 keys → 401** |

### Leitura

Correlação de ~1h entre a exposição das keys em repo público e a morte de
**todas** as keys juntas. A explicação mais provável: **detecção automática de
vazamento pelo Google** (o GitHub varre repos públicos em busca de credenciais
e notifica o provedor; keys expostas são desativadas).

Hipótese alternativa (o corpo do 401 agora é capturado pelo teste atualizado —
rodem de novo com keys novas se persistir): **ação em nível de conta** — usar
36 keys para multiplicar a cota do free tier contraria os ToS do Google, e a
conta/AI Studio pode ter sido limitada. O texto integral do erro distingue as
duais causas ("API key not valid" = key desativada; texto de suspensão =
conta).

**Correção de registro:** a nota "suas keys provavelmente seguem vivas" da
seção 5.3 estava errada — a validação remota de 22:45 já via o 401; naquele
canal GET sem header o 401 era indistinguível de artefato de formato. O teste
real na máquina do usuário resolveu a ambiguidade.

### Lições e ações

1. **ROTACIONAR AGORA**: descartar as 36 keys expostas no AI Studio e criar
   keys novas. Keys expostas em repo público consideram-se comprometidas
   para sempre — mesmo que o repo vire privado depois.
2. As keys novas **nunca** vão para o git: `config.json` fica fora
   (`.gitignore`), o zip com as antigas permanece no histórico (commit
   `bcd8f34`) enquanto o histórico não for purgado / repo não for privado.
3. Com keys novas, rodar o `.bat` de novo: o **P2** responde a pergunta da
   cota compartilhada (seção 2) e o plano F1–F3 sai do papel. Se o 401
   persistir com keys novas, o corpo do erro (agora capturado) dirá por quê.
4. **Reduzir a frota**: se todas as keys compartilham o mesmo projeto, 36
   keys não multiplicam cota nenhuma — só multiplicam a superfície de
   vazamento (36 segredos para vigiar em vez de 4) e o custo do failover
   (teto de 36–144 tentativas por pedido, bug B2). Menos keys, com o
   roteador contando cota do jeito certo (F1), dá o mesmo resultado com
   menos risco.
