# RELATÓRIO DE AUDITORIA — Monitor-Google (gateway Gemini)

> Auditoria estática + forense de dados. **Nenhuma linha de código foi alterada.**
> Data: 23/08/2026 · Janela de dados analisada: 16/08 a 23/08/2026 (banco `historico.db`, 5.571 requisições registradas, 194.741 snapshots).
> Fonte de verdade da API usada nesta auditoria: `RELATORIO_GEMINI_API.md` (coleta da documentação oficial de 16/08/2026, seções citadas como §).

---

## 0. Resumo executivo

| # | Achado | Frente | Evidência-chave | Impacto medido |
|---|--------|--------|-----------------|----------------|
| A1 | **Banco de dados sem permissão de escrita** — o servidor roda com uma conta que só tem leitura em `historico.db`; **toda gravação é descartada** | 1 e 3 | ACL: `CodexSandboxUsers:(RX)`; logs: `attempt to write a readonly database` (`servidor.erros.log`, `servidor.log`) | Contador de cota congelado → roteador acha que há folga infinita → **76 tentativas por sucesso** no dia 21/08 |
| A2 | **Corrida de chaves (hedging ×3)** — padrão do código dispara o mesmo pedido em 3 chaves simultâneas; cada cópia consome cota | 1 | `app/config.py:34` (`"chaves_por_tentativa": 3`); teste oficial `testes/teste_gateway.py:524-562` ("1 pedido queima 3 cotas"); 565 segundos com **≥3 respostas 429 no mesmo instante** | Multiplica consumo de upstream por exatamente N (3×) mesmo em sistema saudável |
| A3 | **Polling inútil de `GET /models`** — 37 chamadas/minuto ao Google que **não retornam nenhum dado de uso** | 1 | `app/agendador.py:28-70`, `app/gemini_api.py:156-166`; o "uso" vem 100% do banco local (`db.uso_bruto`) | ~**53.280 chamadas/dia** (≈2,2 mi/semana) contra ~123 requisições úteis/dia |
| B1 | **Tempestade de retentativa sem backoff real** — erro 5xx/rede não bloqueia a chave (config promete `cooldown_net_seg`, mas ele é **código morto**) | 2 | `app/proxy.py:443` (`marcar_erro` sem cooldown); grep prova que `cooldown_net_seg`/`cooldown_429_max_seg` nunca são lidos | Pico de **225 tentativas upstream/minuto** (09:26 21/08) contra limite de 20 RPM/chave |
| B2 | **Um pedido do cliente caminha por TODAS as chaves** antes de falhar, e o deadline configurado é sobrescrito a cada rodada | 2 e 3 | `app/proxy.py:338` (filtro `usadas`), `app/proxy.py:310` vs `:388` (overwrite do deadline) | Até 51 tentativas por 1 pedido; `max_espera_requisicao_seg` só vale na 1ª rodada |
| B3 | **Backoff exponencial zerado a cada sucesso** — `marcar_sucesso` apaga o histórico de erros que alimenta o multiplicador | 2 | `app/estado.py:88-94` (`erros_recentes.pop`) vs `app/proxy.py:434-437` | Em tráfego misto, a chave volta a ser martelada no intervalo base (15 s) indefinidamente |
| C1 | **Contadores locais ignoram falhas** — janelas contam só `ok=1`; o Google conta 400/500 contra cota (§7.6, §10.3) | 3 | `app/db.py:187` (`WHERE ... ok = 1`) | Subcontagem sistemática → roteador superestima folga → mais 429 |
| C2 | **SQLite: conexão nova por operação, sem índice nas consultas de janela** | 3 | `app/db.py:14-20` (`conectar()` em toda função), único índice `idx_req_id(id)` (`db.py:56`) enquanto 8 queries filtram por `quando`/`chave` | Full scan crescente; UI consulta 6 endpoints a cada 10 s (`site/app.js:11`) |
| C3 | **Snapshots sem retenção** — 36 linhas/minuto para sempre | 3 | `agendador.py:69`; nenhum `DELETE` em `db.py` | 194.741 linhas / **89,8 MB em 7 dias** → projeção ~18,9 M linhas (~8,7 GB/ano) |
| C4 | **Sem reuso de conexão HTTP** — novo TCP+TLS a cada chamada (`urllib` puro) | 3 | `app/gemini_api.py:13-21` | ~100-300 ms extras por chamada (handshake); 5.571 chamadas/semana ≈ 9-28 min só de handshake |

**Eficiência global medida: 860 sucessos / 5.571 tentativas = 15,4%.**
Num sistema saudável esse número deveria ser ≥ 90% (1 tentativa por sucesso + failover raro).
Ou seja: **~84% de todo o tráfego de geração enviado ao Google na semana foi desperdício**, além das ~2 milhões de chamadas de monitoramento.

---

## 1. Escopo, método e inventário

### 1.1. O que o sistema é
Gateway OpenAI-compatível para a Gemini API (`https://generativelanguage.googleapis.com/v1beta/openai`) com:
- **Monitor local de cota**: soma tokens/requisições no SQLite e compara com limites do `config.json` (o Google não expõe % de uso por API — README, "NOTAS").
- **Roteador multi-chave**: escolhe a chave com mais folga (`app/roteador.py`), faz failover em erro (`app/proxy.py:_abrir`).
- **Agendador**: thread que "valida chaves" e grava snapshots a cada `intervalo_uso_seg` (60 s).
- **Painel web** (`site/`) + CLI (`monitorar.py`) + supervisor (`launcher.py`).
- Config atual: **36 chaves ativas**, `rpm=20`, `tpm=1.000.000`, `requisicoes_dia=20`, `tokens_mes=20M`, `chaves_por_tentativa=1`, `host=0.0.0.0`, `gateway_token=""`.

### 1.2. Como investiguei
1. Leitura integral dos módulos (`app/*.py`, `monitorar.py`, `launcher.py`, `servidor.py`, `site/app.js`, `testes/`).
2. Forense do SQLite: distribuição de status, rajadas por segundo/minuto, razão tentativas×sucesso por dia, crescimento de snapshots.
3. Logs: `servidor.log`, `servidor.erros.log`, `logs/launcher.log`.
4. Arqueologia de configuração: `config.json.bak-antes-dedupe` (51 chaves), `config.json.bak-antes-4-6` (3 chaves, RPD 1500).
5. ACL do banco (`icacls`) e processos vivos.
6. Pesquisa: **sem acesso à rede neste ambiente**; usei o snapshot oficial já no repositório (`RELATORIO_GEMINI_API.md`). Os dois pontos que merecem re-confirmação online estão marcados com ⚠ na seção 7.

---

## 2. FRENTE 1 — Onde a cota está sendo queimada à toa

### A1. Banco somente-leitura: o defeito nº 1 (grave, ativo agora)

**Evidência direta:**
```
servidor.erros.log / servidor.log (recorrentes):
  escrita no banco ignorada (banco somente-leitura?): attempt to write a readonly database
```
```
icacls historico.db:
  COMPUTADOR\CodexSandboxUsers:(RX)      <- só leitura/execução
  COMPUTADOR\User:(F)
```
O processo servidor já rodou sob conta sem direito de escrita no arquivo. Como o código **engole o erro silenciosamente** (`app/db.py:82`: `log.warning(...); return`), o gateway continua no ar "funcionando", mas:

- `registrar_requisicao` → nada é gravado;
- `registrar_snapshot` → nada é gravado;
- `uso_bruto()` (`db.py:176-196`) passa a ler contadores **congelados**;
- janelas dia/mês nunca enchem → `_elegiveis` nunca corta chave esgotada → o roteador só descobre esgotamento **recebendo 429 do Google**.

**Prova matemática do efeito (dia 21/08):**
- Tentativas: 2.280 · Sucessos: 30 · Razão = **76 tentativas por sucesso**.
- Com contadores funcionando, a chave que já gastou os 20 RPD seria cortada proativamente (`percent_ativo >= LIMITE_ESGOTADA`, `roteador.py:19`); o mesmo dia custaria ≤ ~40 tentativas.
- Excesso atribuível ≈ 2.240 tentativas num único dia, todas rejeitadas com 429 (cada uma ainda ocupa o RPM momentâneo e cria carga que o Google monitora contra abuso, §15).

**Por que isso importa além do desperdício:** o design inteiro do monitor é "contar localmente porque o Google não expõe uso". Se o contador não escreve, o sistema vira um loop puramente reativo a 429 — o pior modo possível para a reputação do IP/app junto ao abuse monitoring (§15).

### A2. Corrida de chaves (`chaves_por_tentativa`): multiplicador exato de cota

**Código:** `proxy.py:309` `n_racers = max(1, int(cfg.get("chaves_por_tentativa", 3)))` → dispara N threads simultâneas com o MESMO corpo (`proxy.py:365-386`); a primeira resposta 2xx ganha, as outras são lidas e descartadas (`_registrar_descartada`, `proxy.py:266-296`).

**O próprio teste do repositório documenta o custo** (`testes/teste_gateway.py:524-545`):
> "com chaves_por_tentativa=3, 1 pedido consume 3 cotas do backend"

**Matemática:**
- Sistema saudável (p_sucesso alto): E[tentativas upstream] = **N sempre** (todas as N chegam ao Google; cada 2xx consome tokens/RPM/RPD; cada 400/500 também conta contra cota — §7.6, §10.3).
- Sequencial com failover: E[tentativas] = 1 + p_falha + p_falha² + … ≤ 1/(1−p_falha), e na prática ≈ 1,05.
- Com N=3: **overhead de +200% de cota de upstream em regime normal** — justamente quando não há problema algum para "cobrir".

**Evidência nos dados:** 565 segundos distintos com **≥3 respostas 429 no mesmo segundo** (ex.: 08:03:07 → Google-20, -22, -23; 08:04:53 → -24, -25, -28 …) = assinatura inequívoca do fan-out ×3.

**Status atual:** `config.json:11` já fixa `chaves_por_tentativa: 1` (alguém mitigou), **mas** `PADRAO` em `config.py:34` continua 3. Qualquer config novo/regenerado ou chave removida do JSON volta a queimar 3×. É dívida de regressão.

### A3. Polling de `GET /models` que não produz informação nenhuma

**Código:**
- `agendador._loop` (`agendador.py:28-34`) roda `atualizar_tudo()` a cada 60 s;
- `atualizar_tudo` chama `modelos.sincronizar(cfg)` (**GET /models** com a 1ª chave ativa que responder, `modelos.py:46`) **e depois** `atualizar_chave(chave)` para **cada** uma das 36 chaves — cada uma fazendo outro GET /models (`gemini_api.validar_chave`, `gemini_api.py:156-166`);
- o resultado dessa chamada é descartado para fins de uso: `resp.read()` e nada mais (`gemini_api.py:159`).

**Onde o "% de uso" realmente nasce:** `db.uso_bruto(nome)` + `janelas.calcular` (`agendador.py:65-69`). Ou seja, **100% da informação de uso vem do banco local; o Google não devolve nada no polling**.

**Matemática do desperdício:**
- Por ciclo (60 s): 1 (sync) + 36 (validar) = **37 GET/min** = **53.280/dia** ≈ 2,22 milhões/semana.
- Tráfego útil real (sucessos de chat): 860/semana ≈ 123/dia.
- **Proporção ruído:sinal ≈ 62 : 1 por dia; ≈ 2.580 : 1 na semana.**

⚠ Nuance honesta: o README afirma que `GET /models` "não gasta cota". Não consegui re-verificar online (ambiente sem rede). Mesmo no cenário mais favorável (bucket de quota separado para ListModels), restam: 53 mil chamadas HTTP/dia de carga inútil, handshakes TLS, entradas de log, risco de colisão com limites por método e um padrão de acesso mecânico idêntico-em-todos-os-projetos que ajuda nada na relação com abuse monitoring. A recomendação (seção 6) independe dessa dúvida.

**Agravante:** `validar_chave` usa timeout de até 60 s por chave e é sequencial no CLI (`monitorar.py:49-63`): 36 chaves × timeout = até 12 min para uma checagem manual.

### A4. Falhas não contam nas janelas locais (subcontagem estrutural)

`uso_bruto` filtra `ok = 1` (`db.py:187`). Mas a documentação oficial coletada diz:
- "Request que falha com **400 ou 500**: não cobrada, mas **conta contra cota**." (RELATORIO_GEMINI_API.md §7.6, §10.3)

Consequência: quanto pior o momento (mais 400/503), mais otimista fica o contador local, exatamente quando o roteador precisaria de conservadorismo. 429 é rejeição (não debita RPD adicional), mas 400/404/416/422 repassados e 5xx tentados deveriam entrar na contagem de tentativas do minuto/dia.

### A5. Pequenos queimadores
- `reasoning_effort: "medium"` injetado em **todo** pedido gemini que não trouxer um (`config.py:36`, `proxy.py:_corpo_com_esforco`) → tokens de raciocínio em tudo, consumindo TPM/tokens-mês (20 M) mais rápido. É decisão legítima, mas deveria ser consciente/opt-in por cliente, não default universal.
- `_corpo_com_fechamento` anexa `{"role":"user","content":"Continue."}` quando o histórico termina em turno do assistente (`proxy.py:179-201`). Contorno necessário (a API recusa turno final do modelo), mas gera uma continuação faturada que o usuário não pediu. Alternativa: instrução mínima tipo `(continue)` já existe; avaliar limitar `max_tokens` nesse caso específico.
- `max_conc_por_key=2` + corrida ×3: um único pedido chegou a ocupar slot de concorrência em 3 chaves ao mesmo tempo.

---

## 3. FRENTE 2 — Maneiras erradas de chamar o Google (travamentos e 429 em cascata)

Regra oficial de retry (§7.6): *retry SOMENTE em 429, 408, 5xx; backoff exponencial + jitter; máximo de tentativas definido; nunca retry cego em 400/401/403.*

### B1. 5xx e erros de rede não geram cooldown nenhum

- `CODES_COOLDOWN = (408, 429)` e 401/403 invalidam, mas **500/503/504 caem em `marcar_erro`** (`proxy.py:439`) que **não define `cooldown_ate`** (`estado.py:61-66`). A chave continua elegível no próximo ciclo do `while True`.
- Erros de rede (`URLError`, timeout) idem (`proxy.py:441-444`).
- O `config.json` tem `cooldown_net_seg: 30` **que nunca é lido em lugar nenhum** (grep: só definição em `config.py:31` e menção no README). Código morto — a intenção existia, a implementação não.

**Evidência:** 508 respostas 503 registradas; mensagem recorrente "This model is currently experiencing high demand" — o cenário exato em que o §7.6 manda esperar com backoff. O sistema instead re-tenta a próxima chave em ~0 ms, e a mesma chave no próximo pedido.

**Resultado medido (21/08, 09:23-09:31):** minutos com 225/222/184/167 tentativas — a fila de pedidos do opencode empilhada, cada uma varrendo dezenas de chaves, enquanto o Google respondia "high demand". Travamento percebido pelo usuário + queima total do minuto-RPM do fleet.

### B2. Varredura completa do pool + deadline mutante

- Um pedido acumula `usadas` e só desiste quando **todas** as chaves forem tentadas (`proxy.py:338`); não há teto de tentativas por pedido. Histórico mostra pedidos passando por **51 chaves** (época do bak-antes-dedupe) — hoje seriam 36.
- `deadline` inicial = `inicio + max_espera_requisicao_seg (120 s)` (`proxy.py:310`), mas a cada rodada é **sobrescrito**: `deadline = time.time() + timeout + 5` (`proxy.py:388`). Depois da 1ª rodada, `max_espera_requisicao_seg` não governa mais nada — cada rodada se dá um prazo novo de 125 s. Em incidente (rodadas rápidas de 429), o pedido vive muito além do configurado, segurando thread do threadpool do uvicorn (`endpoints.py:311/343` via `run_in_threadpool`) — **é aqui que o sistema "trava" na prática**.

### B3. Backoff exponencial reinicia a cada sucesso

`estado.marcar_sucesso` faz `erros_recentes.pop(chave)` (`estado.py:88-94`). O multiplicador do 429 transitório usa `len(erros_10min)` (`proxy.py:434-437`). Em tráfego alternado sucesso/falha (o padrão real de RPM estourando), a sequência é: erro→cooldown 15 s→sucesso→**histórico zerado**→erro→cooldown 15 s… O backoff nunca sai do degrau 1, garantindo martelada contínua na mesma chave. Correção sugerida na seção 6 (decair por tempo, não zerar por sucesso).

### B4. Padrão de acesso que o Google não gosta
- **Fan-out síncrono do mesmo payload por várias chaves no mesmo segundo** (565 segundos com ≥3 429 simultâneos) é assinatura de abuso/hedging agressivo; §15 documenta que uso é monitorado contra abuse. Com `gateway_token=""` e `host=0.0.0.0`, qualquer host da LAN pode estar gerando esse tráfego usando sua cota — hoje o padrão observado é consistente com o próprio gateway, mas a porta aberta torna impossível afirmar quem é o autor de cada pico.
- **Headers duplicados de autenticação** (`Authorization: Bearer` **e** `x-goog-api-key` juntos, `gemini_api.py:14-19`). O endpoint OpenAI-compat usa Bearer; x-goog-api-key é da API nativa. Enviar os dois é superfície desnecessária e inconsistente. ⚠ Re-confirmar na doc oficial (§16 do relatório de pesquisa) qual header preferir e usar UM.
- **Retry imediato sem jitter** entre candidatas da mesma rodada (threads disparadas juntas, `proxy.py:384-386`) — o §7.6 recomenda jitter para evitar sincronização de retentativas.

### B5. Cooldown de 429 transitório — acertos e furos
Acertos (mantenha):
- Respeita `retryDelay` do corpo e `Retry-After` (`gemini_api.retry_delay_seg`, `proxy.py:429-433`) ✓
- Distingue cota dia/mês (bloqueio até reset Pacífico) de RPM/TPM (`quota_reset_alvo`, `proxy.py:422-427`) ✓

Furos:
- Teto do backoff está **hardcoded** `300` em `proxy.py:436` enquanto `cooldown_429_max_seg` do config é ignorado (mesmo destino do `cooldown_net_seg`).
- `erros_anteriores = len(erros_10min)` conta **depois** de já ter appendado o erro atual? — não: `marcar_retry` faz o append; a leitura acontece antes (`proxy.py:434`), então o primeiro 429 vê 0 e aplica base×1. OK. Mas ver B3 para o reset indevido.

---

## 4. FRENTE 3 — Ineficiências internas (tempo perdido no próprio sistema)

### C1. Roteador: qualidade do sinal e do score
- `percent_ativo` vem de snapshots gravados no ciclo do agendador → **defasagem de até 60 s** (e infinita sob A1). Entre ciclos, a única proteção são `rpm_cheia`/`tpm_cheia`, que leem `uso_recente` em memória — alimentado **apenas por sucessos** (falhas não entram, `proxy.py` chama `registrar_uso_local` só em caminhos 2xx/descartada).
- Score `pontuar` (`roteador.py:43-48`): headroom×10 − em_voo×2 − erros×20. Chave sem dados recebe `efetivo=50`. Funcional, mas com 36 chaves quase simétricas o score decide por ruído; o `-20/erro` é apagado a cada sucesso (ver B3).
- `limites_rpm`/`limites_tpm` calculados **uma vez** antes do loop (`proxy.py:313-314`): chave adicionada via painel durante a vida do processo não entra nesses mapas até novo pedido (mapas por pedido, mas construídos no início de cada `_abrir` — ok por pedido; risco menor apenas de consistência).

### C2. Banco de dados
- `conectar()` abre conexão nova (e roda `PRAGMA journal_mode=WAL`) **em cada operação** (`db.py:14-20`, usado por todas as funções) — incluindo 2-3 conexões dentro de um único `uso_bruto` (uma por janela!), chamado 36× por minuto pelo agendador.
- Índices: `requisicoes` só tem `idx_req_id(id)` (`db.py:56`). Todas as queries de janela filtram `quando >= ?` e/ou `chave = ?` (`db.py:157,168,186-190,238-247,261`) → **full table scan**. Hoje (5,5k linhas) é rápido; com retenção zero vai degradar linearmente. UI polla `/api/resumo`+`/api/requisicoes`+`/api/uso?limite=600` etc. **a cada 10 s** (`site/app.js:11,79-100,511`), multiplicando os scans.
- `registrar_requisicao` é síncrono no caminho quente do proxy (bloqueia a thread que segura o pedido do cliente).
- Sem `DELETE` em lugar nenhum: snapshots 194.741 linhas/89,8 MB em 7 dias → ~18,9 M linhas/~8,7 GB por ano (densidade medida: 461 B/linha).

### C3. Rede
- `urllib.request` sem pool: TCP+TLS novos a cada chamada. Estimativa (RTT típico ~40-80 ms para o edge do Google; handshake TLS 1.3 ≈ 1 RTT + TCP 1 RTT): **~100-300 ms por chamada** de overhead → 5.571 chamadas ≈ 9-28 min agregados na semana, além de inflar a latência TTFB de cada pedido do usuário.
- Busy-wait: `time.sleep(0.02)` em loop esperando threads (`proxy.py:389-392`) em vez de `Event.wait()`/`join` com prazo — CPU desperdiçada sob concorrência.
- Threads criadas por tentativa sem limite global de concorrência de saída.

### C4. Supervisor/infra
- `launcher.py` mata o serviço após 3 checagens de saúde falhadas (15 s cada ⇒ ~45 s). Uvicorn iniciando sob carga (ou com o banco travado) pode passar de 45 s sem responder `/api/saude` → kill no meio do boot → loop de reinício. O `logs/launcher.log` (278 KB) mostra exatamente loops desses na madrugada de hoje (para o serviço "go", e 1× para "gemini": `codigo 4294967295`).
- Porta default triplicada e divergente: `PADRAO.porta=8787` (`config.py:24`), fallback do `servidor.py`=8011 (`servidor.py:15`), README diz 8787. Hoje salva pelo `config.json` (`porta: 8011`). Se alguém remover a chave `porta`, o launcher passa a vigiar a porta errada e entra em kill-loop.
- Lixo de repositório: arquivo vazio chamado `=` na raiz (artefato de comando), `app/__pycache__/go_api.cpython-313.pyc` órfão (módulo removido).
- Segurança: chaves de API em texto plano no `config.json`; `host: 0.0.0.0` + `gateway_token: ""` expõe um **proxy aberto de queima de cota** para toda a LAN (`endpoints.py:41-46` libera tudo sem token quando vazio).

---

## 5. Comprovação matemática consolidada

Dados: `historico.db`, 16-23/08/2026.

**5.1. Eficiência de geração (por dia):**

| Dia | Total | OK | Razão (tentativas:sucesso) | 429 |
|---|---|---|---|---|
| 16/08 | 591 | 236 | 2,5× | 302 |
| 17/08 | 639 | 178 | 3,6× | 247 |
| 18/08 | 896 | 143 | 6,3× | 698 |
| 19/08 | 386 | 121 | 3,2× | 22 |
| 20/08 | 45 | 20 | 2,2× | 18 |
| **21/08** | **2.280** | **30** | **76,0×** | **2.231** |
| 22/08 | 636 | 89 | 7,1× | 496 |
| 23/08 | 98 | 43 | 2,3× | 39 |
| **Total** | **5.571** | **860** | **6,48×** | **4.053 (72,7%)** |

Modelo saudável: E[tentativas/sucesso] ≈ 1,05 (failover raro). Excesso semanal ≈ 5.571 − 860×1,05 ≈ **4.668 tentativas desperdiçadas = 83,8% do tráfego de geração**.

**5.2. Fan-out da corrida:** 2.532 segundos com algum 429; **565 (22,3%) com ≥3 429s no mesmo segundo** — compatível com N=3 simultâneos por pedido.

**5.3. Polling:** 37 GET/min × 1.440 min = **53.280 GET/dia**; semana ≈ **2,23 M GET**. Sinal útil diário ≈ 123 OK. Ruído:sinal semanal ≈ **2.580×**.

**5.4. Armazenamento:** 194.741 snapshots / 7 dias = 27.820/dia (≈36 chaves × 60 s ✓). Tamanho 89.776.128 B ÷ 194.741 = **461 B/linha**. Projeção anual: 10,2 M-18,9 M linhas (dependendo de K) ≈ **4,7-8,7 GB/ano**.

**5.5. Pico de tempestade:** 225 tentativas/min (09:26 21/08) sobre 51 chaves = 4,4 tentativas/chave/min sustentadas por minutos, 100% delas respostas de erro.

**5.6. Latência de handshake (estimativa conservadora):** 2 RTT (TCP+TLS1.3) × ~60 ms = ~120 ms/call × 5.571 calls ≈ **11 min puros de handshake na semana** (fora os 2,23 M GETs de polling ≈ 74 h agregados se considerados — dominância total sobre a carga útil).

---

## 6. TUTORIAL DE MUDANÇA (ordem de aplicação, verificação e rollback)

> Regras gerais: aplicar **uma fase por vez**, medir 24 h entre fases com as queries da seção 7. O projeto **não é git** — antes de tudo, tire um snapshot: copie a pasta (menos `historico.db*` e `logs/`) ou rode `git init && git add -A && git commit -m "baseline pre-auditoria"`.

### FASE 0 — Sem tocar código (hoje, < 30 min)
**0.1 Devolver escrita ao banco (corrige A1).**
- Escolha: (a) dar Modify ao usuário do serviço: `icacls historico.db /grant "CodexSandboxUsers:M"` (e na pasta, para o WAL criar `-wal/-shm`), ou (b) rodar o launcher/servidor como `User`.
- Verificar: abrir o painel → botão "Atualizar agora" → conferir linha nova em `snapshots` e ausência da msg "readonly" no log por 1 h.
- Rollback: n/a (é correção de permissão).
- **Por que primeiro:** sozinho, este passo derruba a razão tentativas:sucesso de dezenas para poucas unidades, porque os contadores voltam a cortar chave cheia ANTES do 429.

**0.2 Fechar o proxy aberto (A5/C4-segurança).**
- `config.json`: `"host": "127.0.0.1"` (ou mantenha 0.0.0.0 + firewall) e `"gateway_token": "<segredo>"`; regenere o token no cliente (opencode.json).
- Verificar: `curl http://127.0.0.1:8011/v1/models` sem token → 401.

**0.3 Congelar a corrida.**
- Manter `"chaves_por_tentativa": 1` (já está) e anotar para a Fase 3 remover o default 3 do código.

### FASE 1 — Parar o desperdício de polling (A3) — ganho de ~99% no volume total de chamadas
**1.1 Validar chave só quando faz sentido.**
- Onde: `agendador.atualizar_tudo/atualizar_chave`.
- Mudança: validar todas as chaves 1× no boot; depois disso, **nunca** em loop. Invalidade real já é detectada em voo (401/403 → `marcar_invalida`, `proxy.py:420-421`). Opcional: revalidar chaves marcadas inválidas 1×/hora.
- Custo/benefício: 36 GET/min → ~0 GET/min de validação; perda zero de informação de uso (ela nunca veio daí).

**1.2 Sincronizar modelos com cadência humana.**
- Onde: `agendador.atualizar_tudo` + `modelos.sincronizar`.
- Mudança: nova config `modelos_sync_seg` (default 21600 = 6 h); guardar `ultimo_sync_modelos` em memória/arquivo; usar **uma** chave (a primeira ativa). Lista de modelos muda em escala de dias/semanas, não minutos.
- Resultado: 1.440 syncs/dia → **4/dia**.

**1.3 Snapshot com detecção de mudança + retenção.**
- Onde: `agendador.atualizar_chave` (gravar só se percent mudou >1 p.p. OU 5 min desde o último), e novo job de retenção (`DELETE FROM snapshots WHERE quando < now-30d` + `PRAGMA wal_checkpoint(TRUNCATE)` semanal).
- Resultado: 51.840 → ~2-5 k linhas/dia; DB estabiliza em <300 MB.

**Verificação da fase:** SQL da seção 7 (Q3/Q5); meta: GETs ao Google ≈ 4/dia + tráfego de chat.

### FASE 2 — Contadores honestos + banco rápido (A4, C2)
**2.1 Contar tentativas nas janelas.**
- Onde: `db.uso_bruto` (adicionar contagem de tentativas: `COUNT(*)` sem filtro ok, e tokens só de ok) e `janelas.calcular` (usar tentativas para dimensões RPM/RPD; manter tokens por ok). Registrar também falhas em `estado.registrar_uso_local` (peso 1 req, 0 token).
- Justificativa: §7.6/§10.3 — 400/500 contam contra cota.

**2.2 Índices e conexão reutilizada.**
- `CREATE INDEX IF NOT EXISTS idx_req_quando ON requisicoes(quando); CREATE INDEX IF NOT EXISTS idx_req_chave_quando ON requisicoes(chave, quando);` (no `inicializar`).
- Trocar `conectar()`-por-operação por conexão **thread-local** (threading.local) criada once; manter retry de lock.
- Onde: `db.py` inteiro; hot paths beneficiados: `registrar_requisicao`, `balanco_recente_por_chave` (UI a cada 10 s), `uso_bruto`.

**2.3 Fail-visibility do banco.**
- Onde: `db._escrever`.
- Mudança: expor contador de falhas de escrita; `endpoints.saude` inclui `db_ok:false` quando >0 nos últimos 5 min (e log ERROR 1×/min, não por operação). Nunca mais um readonly silencioso por horas.

### FASE 3 — Proxy/roteador conforme o manual do Google (B1-B3, A2)
**3.1 Default da corrida = 1.**
- Onde: `config.py:34` → `"chaves_por_tentativa": 1`. Hedging vira opt-in documentado. (Comportamento já validado pelo teste `teste_gateway.py:550-562`.)

**3.2 Cooldown real para 5xx/rede (usar o config morto).**
- Onde: `proxy.py` ramo final de falhas.
- Mudança: em 500/503/504 e erros net → `estado.marcar_retry(nome, ..., time.time() + cooldown_net_seg × (2^erros_10min) + jitter(0..1 s))`, teto `cooldown_429_max_seg` (substituir o `300` hardcoded). 408 permanece no grupo 429.
- Justificativa: §7.6 (backoff exponencial + jitter; 5xx é retriable MAS não imediatamente e não infinitamente).

**3.3 Deadline absoluto.**
- Onde: `proxy.py:388`.
- Mudança: apagar a linha (usar o `deadline` inicial de `:310`); opcional `min(deadline, now+timeout+5)` por rodada para não matar rodada em curso.
- Teste novo sugerido: pedido com todas as chaves mock retornando 429 instantâneo deve falhar em ≤ `max_espera_requisicao_seg` (+ε).

**3.4 Teto de tentativas por pedido.**
- Onde: `proxy._abrir` loop.
- Mudança: `tentativas_max = min(len(chaves), int(cfg.get("tentativas_por_pedido", 5)))`; ao atingir → `SemChaveDisponivel` com motivos.
- Efeito: pior caso por pedido cai de 36-51 tentativas para 5; incidentes deixam de empilhar threadpool.

**3.5 Não zerar histórico de erros no sucesso.**
- Onde: `estado.marcar_sucesso`.
- Mudança: remover `erros_recentes.pop` (a janela de 10 min já expira sozinha em `erros_10min`); manter limpeza de `erros`/`cooldown`.
- Efeito: backoff de 429 sobrevive a sucessos intercalados.

**3.6 Pool de conexões.**
- Onde: `gemini_api._requisicao`.
- Mudança mínima-dependência: `http.client.HTTPSConnection` por thread com keep-alive (dict thread-local), ou adicionar `httpx` ao requirements com `httpx.Client(limits=..., timeout=...)` global. Medir p95 antes/depois com `db.estatisticas.media_ms_ok`.

**3.7 Header único.** ⚠ confirmar doc (§16) → provavelmente manter só `Authorization: Bearer` no endpoint openai-compat; remover `x-goog-api-key` duplicado.

**Verificação da fase:** repetir o dia de pico em ambiente controlado com `testes/mock_gemini.py`: metas — tentativas/pedido ≤ 5; zero tentativa durante cooldown declarado; `429/min` externo ≈ 0 fora de cota real.

### FASE 4 — Fôlego geral (C3, C4)
- `site/app.js`: `POLL_MS=30000` + pausa com aba oculta (`document.visibilityState`).
- `monitorar.py --watch`: coleta paralela (ThreadPoolExecutor 8) e sono até o próximo tick fixo (sem drift); `--historico` intacto.
- `launcher.py`: janela de graça para startup (não contar falhas de saúde nos primeiros 60 s de vida do processo); unificar default de porta em um só lugar (importar de `config.PADRAO`).
- Housekeeping: apagar arquivo `=`, remover `go_api.cpython-313.pyc`, atualizar README (porta 8011, tabela de configs reais, remover promessa de `cooldown_net_seg` até a Fase 3.2 existir).

---

## 7. Métricas de acompanhamento (cole e rode depois de cada fase)

```sql
-- Q1: eficiência global (meta: razao < 1,3 após Fase 0-3)
SELECT COUNT(*) total, SUM(ok) oks, ROUND(COUNT(*)*1.0/MAX(SUM(ok),1),2) razao FROM requisicoes;

-- Q2: 429 por dia (meta: ~0 fora de esgotamento real de RPD)
SELECT substr(quando,1,10) dia, SUM(CASE WHEN status=429 THEN 1 ELSE 0 END) quatro29
FROM requisicoes GROUP BY dia ORDER BY dia DESC LIMIT 14;

-- Q3: pico por minuto (meta: < 40)
SELECT substr(quando,1,16) m, COUNT(*) n FROM requisicoes
GROUP BY m ORDER BY n DESC LIMIT 5;

-- Q4: snapshots/dia após retenção (meta: < 5000)
SELECT substr(quando,1,10) dia, COUNT(*) n FROM snapshots GROUP BY dia ORDER BY dia DESC LIMIT 7;

-- Q5: latência média dos OK (deve cair ~100ms após pool de conexões)
SELECT AVG(ms) FROM requisicoes WHERE ok=1 AND substr(quando,1,10)=date('now');
```

Metas de sucesso 30 dias: eficiência ≥ 90%; 429 semanais < 50 (vs 4.053); chamadas de monitoramento ≤ 20/dia (vs 53.280); DB < 300 MB estável; zero "readonly database" em log.

---

## 8. Anexo — achados menores e observações
1. `cooldown_net_seg` e `cooldown_429_max_seg` são **config mortos** (grep em `app/` não acha leitores) — ou implementar (Fase 3.2) ou remover do README/config para não mentir ao operador.
2. `endpoints.sincronizar_modelos` e o agendador escrevem `config.json` de threads diferentes sem lock (`config.salvar`) — risco baixo de corrida; mutex simples resolve.
3. `monitorar.py --watch` dorme intervalo **após** o trabalho → drift acumulado; irrelevante para cota, relevante para pontualidade.
4. `db.inicializar` faz `DROP TABLE snapshots` se achar coluna legada `rolling_pct` — migração one-shot ok, mas roda a cada boot (barata; apenas documentar).
5. Chaves de API em texto plano no `config.json` + backups `.bak` **com chaves antigas** — considerar rotação das chaves expostas em `.bak-antes-*` e permissão NTFS restrita no arquivo.
6. `RELATORIO_GEMINI_API.md` §8.1: limites são **por projeto**, não por chave — se várias destas 36 chaves compartilham projeto, a folga real por chave é menor do que o modelo local assume; agrupar chaves por projeto no config seria refinamento futuro do roteador.
7. ⚠ Pendências de re-verificação online (sem rede aqui): (a) tratamento de quota do `GET /models`; (b) header preferido no endpoint OpenAI-compat. Fontes: seção 16 do `RELATORIO_GEMINI_API.md`.

**Fim do relatório. Nenhum arquivo do sistema foi modificado durante esta auditoria** (apenas leituras; scripts de análise ficaram em `%TEMP%\opencode\`).
