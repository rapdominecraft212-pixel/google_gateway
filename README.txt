MONITOR + GATEWAY GOOGLE GEMINI
===============================

Backend completo para monitorar e ROTEAR varias keys da Gemini API
(Google AI for Developers). O gateway fala com o Google pela camada
OFCICIAL de compatibilidade OpenAI (https://generativelanguage.googleapis.com/
v1beta/openai), entao o cliente (opencode etc.) continua usando o
formato OpenAI de sempre - sem traduzir nada na mao.

O QUE FAZ
---------
1. MONITORA (local): o Google NAO tem API de % de uso por janela.
   Em vez disso, o monitor soma localmente o que o gateway registrou
   e compara com os limites configurados por chave:
   - minuto: RPM (requests/min) e TPM (tokens/min)
   - dia:    RPD (requests/dia) e tokens do dia
   - mes:    tokens do mes
   O reset do dia/mes segue a meia-noite do horario do PACIFICO
   (mesma regra das cotas oficiais do Google).
   Alem disso, valida cada key via GET /v1beta/openai/models
   (nao gasta cota) a cada coleta.

2. ROTEIA (gateway): recebe requests OpenAI-compativeis e escolhe
   SEMPRE a key com mais folga. Se uma key responder 429/408/5xx,
   troca NA HORA para a proxima. 401/403 marca a key como invalida.
    400/404/416/422 repassam direto COM o corpo do Google (o erro e do
    cliente, nao da key; erro mudo — status sem corpo — nao pode existir).
    Modelo fora da lista e barrado na borda com 404 local (nunca queima
    cota); se o Google der 404 num modelo listado, o gateway aprende e o
    remove da roteacao permanentemente (modelos_indisponiveis no config).

3. REGISTRA: todas as requisicoes (key, modelo, tokens in/out, tempo,
   erro com o code snake_case do Google) + snapshots das janelas +
   estatisticas para o painel.

ESTRUTURA
----------
  servidor.py          inicia o servidor (uvicorn, porta 8787)
  monitorar.py         CLI: checagem unica / --watch / --historico
  launcher.py          modo servico (auto-restart, trava de porta)
  diagnostico.py       CLI: diagnostico rapido do gateway
  atualizar_modelos.py sincroniza a lista de modelos no config
  config.json          chaves, limites, limiar, porta, modelos
                       (SEGREDO - fica fora do git via .gitignore)
  config.json.example  molde sem keys reais (copie para config.json)
  historico.db         SQLite (criado automaticamente; migra sozinho)
  opencode.json.example  como apontar o opencode para o gateway
  site/                INTERFACE WEB (painel)
  docs/                relatorios (auditoria do sistema + pesquisa da API)
  backups/             snapshots antigos do config.json (fora do git)
  logs/                logs de runtime (fora do git)
  .gitignore           mantem segredos e runtime fora do git
  RODAR-TESTE-COTA.bat  teste de cota real em 2 cliques (Windows/GitHub
                       Desktop): roda teste/teste_cota_real.py e grava o
                       pacote de dados em teste/resultados/ (veja passo 6)
  app/
    config.py          leitura/validacao do config.json
    gemini_api.py      cliente da camada OpenAI-compat do Google
    janelas.py         janelas locais (RPM/TPM/RPD/mes), fusos, limites
    estado.py          estado em memoria (janelas, cooldown, em-voo)
    roteador.py        escolha proativa da key com mais folga
    proxy.py           failover: tenta a melhor, cai p/ proxima em erro
    agendador.py       thread que valida keys e coleta uso local
    endpoints.py       rotas da API (monitoramento + gateway) + /
    main.py            monta o app FastAPI + serve o site/
    db.py              SQLite: snapshots + requisicoes + estatisticas
  teste/               UNICA pasta de testes (regras em teste/README.md)
    teste_onda.py        pacing de ondas: bucket/lease/jitter (codigo real)
    teste_honesto_429.py 429 honesto + SQLite real (codigo real)
    harness_producao.py  conversa real contra o gateway (Google real)
    benchmark.py         carga real (Google real; exige --yes-real)
    simular_tarefa.py    tarefa real multi-etapa (gateway no ar)
    analise_kpis.py      medidor de KPIs (SQLite real; prova das correcoes)
    README.md            regras: centralizacao, sem mock, criar->testar->apagar

PAINEL (interface)
------------------
Depois de subir o servidor, abra no navegador:

    http://127.0.0.1:8787/

Mostra:
  - estatisticas do dia (requisicoes, sucesso, janela mais cheia, media)
  - card por chave: % de cada janela com barra + contagem regressiva
    do reset + status (OK/ALERTA/CRITICO/ERRO/INVALIDA/DESATIVADA) +
    uso de hoje
  - ADICIONAR / DESATIVAR / ATIVAR / EXCLUIR chaves direto no painel
    (cada card tem os botoes e ha um formulario na secao Chaves) -
    as mudancas sao salvas no config.json na hora
  - grafico SVG do historico (janela de minuto/RPM por chave)
  - tabela de requisicoes do gateway (filtro so falhas)
  - configuracao atual do servidor (inclui os limites)
  - botao "Atualizar agora" (forca a coleta na hora)
  - botao "Sincronizar modelos do Google" (busca TODOS os modelos que o
    Google oferece agora e atualiza a lista em /v1/models + config.json)

COMO USAR
---------
1. Se ainda nao tiver, copie o molde (Windows: copy, Linux: cp):
       cp config.json.example config.json
   Depois edite config.json: cole suas keys da Gemini (AI Studio >
   API keys) na lista "chaves" (campos nome/key). Chave vazia =
   ignorada. Ajuste "limites" para os seus rate limits (veja AI
   Studio > Rate limits; ex.: free tier do gemini-3.6-flash ~20 RPM).
   IMPORTANTE - se todas as suas keys sao do MESMO projeto Google
   (e o caso do AI Studio, que cria um projeto unico), deixe
   "cota_compartilhada": true - o Google cobra cota por PROJETO,
   nao por chave, e com o flag o gateway passa a respeitar isso
   (um balde de RPM/TPM para a frota toda, cooldown de grupo no
   429 em vez de queimar a pool; ver docs/DIAGNOSTICO_COTA.md).

2. Suba o servidor (deixe rodando):
     python servidor.py
     (ou servidor.bat)

3. Abra o painel no navegador: http://127.0.0.1:8787/

4. Aponte o opencode para o gateway:
   - O gateway SINCRONIZA a lista de modelos sozinho: ao iniciar e a cada
     coleta, ele pergunta ao Google quais modelos existem agora e expoe
     todos em /v1/models (tambem no painel, botao "Sincronizar modelos").
   - Para o app desktop do opencode mostrar TODOS os modelos no seletor,
     rode uma vez (com suas keys no config.json):
        python atualizar_modelos.py
     Isso grava a lista atual em config.json e no opencode.json.example.
     Para mesclar direto no seu config do opencode:
        python atualizar_modelos.py --opencode %USERPROFILE%\.config\opencode\opencode.json
     (ou copie o bloco "provider" do opencode.json.example para o seu
     opencode.json). Uma unica key (gateway-local) e os modelos ficam
     atras do gateway.

5. CLI rapido (sem servidor):
     python monitorar.py              checagem unica
     python monitorar.py --watch 60   monitora a cada 60s
     python monitorar.py --historico  ultimas coletas

6. Teste de cota real (Windows / GitHub Desktop): 2 cliques em
       RODAR-TESTE-COTA.bat
   Ele roda teste/teste_cota_real.py (--yes-real) e grava o pacote de
   dados em teste/resultados/ - depois e so commitar e pushar pelo
   GitHub Desktop. Custo: ~76 pedidos, ~500 tokens.
   Detalhes e leitura do resultado: docs/DIAGNOSTICO_COTA.md (secao 6).

API (tudo GET, salvo indicado)
------------------------------
  /                      painel (interface web)
  /api/saude             ok/versao/hora
  /api/chaves            chaves (key mascarada) + status/erro/em-voo
  /api/resumo            por chave: % de cada janela + reset +
                         status (ok/alerta/critico/invalida) +
                         requisicoes de hoje
  /api/uso?chave=N       historico das coletas de uso
  /api/requisicoes       log das requisicoes do gateway
  /api/estatisticas      totais: requisicoes, ok/falhas, tokens, ms
  /api/configuracao      valores atuais do config.json (sanitizado)
  POST /api/atualizar    forca coleta agora (o agendador roda sozinho
                         a cada intervalo_uso_seg)
  POST /api/chaves          adiciona uma chave {nome, key, tipo}
  DELETE /api/chaves/{nome} exclui uma chave (salva no config.json)
  PUT /api/chaves/{nome}    {"ativa": true|false} desativa/ativa uma
                            chave sem apagar; chave desativada fica
                            fora do roteamento e da coleta de uso
  /v1/models             lista de modelos p/ o cliente
  POST /v1/chat/completions  endpoint OpenAI-compativel (gateway)

CONFIG (config.json)
--------------------
  porta                porta do servidor (8787)
  intervalo_uso_seg    coleta a cada N segundos (60)
  limiar_alerta        % que vira "alerta" no resumo (80)
  max_conc_por_key     maximo de streams simultaneos por key (2)
cooldown_429_seg     espera padrao quando um 429 de RPM/TPM NAO traz
                       RetryInfo/Retry-After (15). Sem backoff exponencial:
                       se o Google informar o tempo de espera, esse tempo
                       exato e respeitado. A key que falhou nunca e
                       retentada no mesmo request (troca na hora).

CLASSIFICACAO DE FALHAS (por chave):
   cota (429 diaria/mensal/billing) -> key fica fora ATE O RESET
       (meia-noite do Pacífico / dia 1º). Nao e tentada de novo.
   retry (429 de RPM/TPM)           -> key fora so pelo tempo exato
       que o Google mandou (RetryInfo/Retry-After).
   transitorio (503 "high demand",
       erro de rede/timeout)        -> NAO bloqueia: so troca para a
       proxima key. Pode ser usada de novo no proximo request.

espera_cooldown_max_seg  quando TODAS as keys estao em cooldown curto
                       (retry) OU com janela cheia (ex.: RPM do minuto),
                       o gateway espera o mais curto expirar (ate este
                       teto) e tenta de novo. Se TODAS estao com COTA
                       esgotada, NAO espera: responde logo "cota
                       esgotada em todas as chaves; reseta em X".
                       em vez de devolver erro na hora (30). Teto curto
                       de proposito: nunca segura o request em espera
                       longa — se nenhuma key liberar neste tempo,
                       responde 503 e o cliente decide o backoff
  timeout_http_seg     timeout dos requests ao Google (120)
  gateway_token        opcional: exige "Authorization: Bearer <token>"
                       nas rotas /v1/* (protege o proxy)
  chaves               lista de {nome, key, tipo, ativa}
                       tipo "gemini" (default)
                       tipo "generico" = sem coleta de uso, roteia so
                       por erro
                       ativa false = desativada: nao roteia, nao coleta
                       uso, mas continua salva no config.json
                       override por chave (opcional): limite_rpm,
                       limite_tpm, limite_requisicoes_dia,
                       limite_tokens_dia, limite_tokens_mes
  limites              limites GLOBAIS por chave:
                       rpm, tpm, requisicoes_dia, tokens_dia,
                       tokens_mes (zero = nao cobrar aquela dimensao)
modelos              modelos expostos em /v1/models (repasse ao
                        Google, ex.: gemini-3.6-flash). A lista e
                        ATUALIZADA automaticamente com todos os modelos
                        que o Google estiver oferecendo (sincronizacao
                        no iniciar + a cada coleta + botao no painel +
                        atualizar_modelos.py)

TESTES
------
Tudo de teste mora na pasta unica `teste/` (regras em `teste/README.md`).
  python -m unittest discover -s teste -t .

Sem mock: exercitam apenas o codigo de producao real (funcoes puras de
pacing/estado + SQLite real em disco). O caminho de rede e validado
contra o Google real por `teste/harness_producao.py` e por
`teste/benchmark.py --yes-real` (consomem cota; nunca na suíte de regressão).

NOTAS
-----
- As janelas de % sao LOCAIS (calculadas do historico do gateway) e
  comparadas com os limites que VOCE configura - o Google nao expoe
  % de uso por API, so telas no AI Studio.
- Erros do Google trazem code snake_case no corpo
  (rate_limit_exceeded, quota_exceeded, authentication...); o painel
  mostra esse code no log de requisicoes.
- Falha NO MEIO de um stream nao da replay invisivel. O gateway
  minimiza escolhendo por folga antes de comecar; troca invisivel
  acontece em 429/401/timeout no inicio.
