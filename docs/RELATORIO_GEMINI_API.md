# Relatório — Google Gemini API (Google AI for Developers)

> Pesquisa completa da documentação oficial (`ai.google.dev/gemini-api/docs`), coletada em 16/08/2026.
> Cobre: estrutura da API, como chamar modelos, mensagens de erro, cotas, rate limits, como ver uso/gastos, billing e preços.

---

## 1. Visão geral

A **Gemini API** é o serviço de IA generativa do Google. Atualmente o Google recomenda a **Interactions API** (GA — geralmente disponível) como a forma oficial de acessar todos os modelos e features mais recentes.

- **Entrada multimodal**: texto, imagem, áudio, vídeo, documentos (PDF) e combinações.
- **Saídas**: texto, imagem (Nano Banana), fala (TTS), música (Lyria), vídeo (Veo/Omni).
- **Base REST**: `https://generativelanguage.googleapis.com`
- **Autenticação**: header `x-goog-api-key: <API_KEY>`
- **SDKs oficiais**: `google-genai` (Python), `@google/genai` (JS/TS), `google.golang.org/genai` (Go), `com.google.genai` (Java).

Existem **duas APIs de geração**:
1. **Interactions API** (`POST /v1beta/interactions`) — nova, GA, recomendada. Estado de conversa no servidor, steps, agents, background execution.
2. **generateContent** (`POST /v1beta/models/{model}:generateContent`) — API clássica por modelo, ainda usada em muitos guias (safety, tokens).

### 1.1. Primeira chamada (mínimo absoluto)

```bash
curl -X POST "https://generativelanguage.googleapis.com/v1beta/interactions" \
  -H "x-goog-api-key: $GEMINI_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{ "model": "gemini-3.6-flash", "input": "Explain how AI works" }'
```

Python:
```python
from google import genai
client = genai.Client()                    # lê GEMINI_API_KEY do ambiente
interaction = client.interactions.create(
    model="gemini-3.6-flash",
    input="Explain how AI works in a few words",
)
print(interaction.output_text)
```

JavaScript:
```javascript
import { GoogleGenAI } from "@google/genai";
const ai = new GoogleGenAI({});
const interaction = await ai.interactions.create({
  model: "gemini-3.6-flash",
  input: "Explain how AI works",
});
console.log(interaction.output_text);
```

### 1.2. Estrutura da resposta (Interaction)

```json
{
  "id": "v1_ChdpQUFvYXI...",
  "status": "completed",
  "usage": {
    "total_tokens": 197,
    "total_input_tokens": 8,
    "total_output_tokens": 12
  },
  "created": "2026-06-09T12:01:25Z",
  "steps": [
    { "type": "thought", "signature": "EvEFCu4FAQw..." },
    { "type": "model_output",
      "content": [ { "type": "text", "text": "AI learns patterns from data..." } ] }
  ],
  "object": "interaction",
  "model": "gemini-3.7-flash"
}
```

- `steps` = histórico passo-a-passo do turno (`thought`, `model_output`, `function_call`, `google_search_call`, `function_result` etc.).
- `usage` = contagem de tokens do turno (não cobrado à parte; é métrica).
- Conveniências dos SDKs: `interaction.output_text` (último(s) bloco(s) de texto), `interaction.output_image`.
- `status` possíveis: `in_progress`, `completed`, `requires_action` (aguarda resultado de função), `failed`.

---

## 2. Chaves de API (API keys)

### 2.1. Onde criar
- **Google AI Studio → página API keys**: `https://aistudio.google.com/apikey`
- Usuário novo: o AI Studio **cria um projeto Google Cloud + chave automaticamente** ao aceitar os ToS.
- Projetos existentes do Google Cloud precisam ser **importados** (Dashboard → Projects → Import projects) para criar chaves neles.

### 2.2. Tipos de chave (transição importante em andamento)

| Tipo | Descrição |
|---|---|
| **Standard key** | Associada a um projeto Google Cloud. Não identifica o chamador. |
| **Authorization (auth) key** | Vinculada a uma **service account** do Cloud; requests rodam sob a identidade dela. Restrita à Generative Language API por padrão. Enforça bloqueio rápido de chaves vazadas. |

Regras de transição:
- **Chaves novas criadas no AI Studio já são auth keys por padrão.**
- A API **rejeita standard keys sem restrição** ("unrestricted"). Standard keys **com restrições aplicadas** continuam funcionando.
- **Em setembro de 2026**: a API vai rejeitar **todas** as standard keys — é obrigatório migrar para auth keys.

### 2.3. Permissões IAM necessárias para criar chave
Se o botão "Create API key" aparecer bloqueado ("You do not have permission to create a key in this project"), falta papel (ex.: Project Editor) com:
- `resourcemanager.projects.get`
- `apikeys.keys.create`
- `serviceusage.services.enable`
- `iam.serviceAccounts.create`
- `iam.serviceAccountApiKeyBindings.create`

### 2.4. Como passar a chave
- **Env vars (recomendado)**: `GEMINI_API_KEY` ou `GOOGLE_API_KEY` (se ambas existirem, `GOOGLE_API_KEY` vence). SDKs detectam automaticamente.
- **No código**: `genai.Client(api_key="...")` / `new GoogleGenAI({ apiKey: "..." })`.
- **REST**: header `x-goog-api-key`.

### 2.5. Restrições e segurança
- Restringir origem (IPs, sites) e APIs permitidas em **Cloud Console → Credentials** (ou direto no AI Studio: "Restrict to Gemini API only").
- Requer permissão `apikeys.keys.update` (papel API Keys Admin ou Editor).
- **Nunca** expor chave em código client-side — usar proxy de backend.
- **Chave vazada**: o Google bloqueia proativamente chaves conhecidas como vazadas e devolve erro `"Your API key was reported as leaked. Please use another API key."` — gerar chave nova no AI Studio.

---

## 3. Modelos disponíveis (endpoints)

### 3.1. Texto (text-out)

| Modelo | Endpoint | Notas |
|---|---|---|
| Gemini 3.7 Flash | `gemini-3.7-flash` | Mais novo e capaz Flash (agentic/coding) |
| Gemini 3.6 Flash | `gemini-3.6-flash` | Geração anterior, equilíbrio velocidade/multimodal |
| Gemini 3.5 Flash | `gemini-3.5-flash` | Legacy Flash |
| Gemini 3.5 Flash-Lite | `gemini-3.5-flash-lite` | Mais rápido/custo-benefício da família 3.5 |
| Gemini 3.1 Flash-Lite | `gemini-3.1-flash-lite` | Descontinuação: **7 maio 2027** → migrar p/ `gemini-3.5-flash-lite` |
| Gemini 3.1 Pro (Preview) | `gemini-3.1-pro-preview` | Inteligência avançada/agentic |
| Gemini 3 Flash (Preview) | `gemini-3-flash-preview` | → migrar p/ `gemini-3.6-flash` |
| Gemini 2.5 Pro | `gemini-2.5-pro` | Família 2.5 mais avançada |
| Gemini 2.5 Flash | `gemini-2.5-flash` | Melhor preço-performance da 2.5 |
| Gemini 2.5 Flash-Lite | `gemini-2.5-flash-lite` | |
| Gemini 2.0 Flash / Flash-Lite | `gemini-2.0-flash` / `gemini-2.0-flash-lite` | **Shut down** |

### 3.2. Imagem (Nano Banana)

| Modelo | Endpoint |
|---|---|
| Nano Banana 2 | `gemini-3.1-flash-image` |
| Nano Banana 2 Lite | `gemini-3.1-flash-lite-image` |
| Nano Banana Pro | `gemini-3-pro-image` |
| Nano Banana (2.5) | `gemini-2.5-flash-image` |
| Imagen 4 (Deprecated) | `imagen-4.0-generate` |

### 3.3. Áudio / fala / música / vídeo

| Modelo | Endpoint |
|---|---|
| Gemini 3.1 Flash Live (A2A em tempo real) | `gemini-3.1-flash-live-preview` |
| Gemini 2.5 Flash Live | `gemini-2.5-flash-native-audio-preview-12-2025` |
| Gemini 3.1 Flash TTS | `gemini-3.1-flash-tts-preview` |
| Gemini 2.5 Flash TTS | `gemini-2.5-flash-preview-tts` |
| Gemini 2.5 Pro TTS | `gemini-2.5-pro-preview-tts` |
| Gemini 3.5 Live Translate | `gemini-3.5-live-translate-preview` |
| Lyria 3 Pro / Clip / RealTime | `lyria-3-pro-preview` / `lyria-3-clip-preview` / `lyria-realtime-exp` |
| Veo 3.1 / 3.1 Lite | `veo-3.1-generate-preview` / `veo-3.1-lite-generate-preview` |
| Gemini Omni Flash (vídeo conversacional) | `gemini-omni-flash` |

### 3.4. Embeddings / especializados

| Modelo | Endpoint |
|---|---|
| Gemini Embedding 2 (multimodal) | `gemini-embedding-2-preview` |
| Gemini Embedding | `gemini-embedding-001` |
| Computer Use | `gemini-2.5-computer-use-preview-10-2025` |
| Deep Research / Max | `deep-research-preview-04-2026` / `deep-research-max-preview-04-2026` |
| Antigravity Agent | `antigravity-preview-05-2026` |

### 3.5. Versões de modelo (naming)

| Sufixo | Significado |
|---|---|
| **stable** | Versão fixa, não muda. Uso recomendado em produção. Ex.: `gemini-3.6-flash` |
| **preview** | Pode ser usado em produção; billing habilitado; rate limits mais restritos; deprecação com **aviso mínimo de 2 semanas**. |
| **latest** | Aponta sempre para a release mais nova da variação (stable/preview/experimental). Hot-swap a cada release; mudanças com breaking change avisam por e-mail com 2 semanas. Ex.: `gemini-flash-latest` |
| **experimental** | Não recomendado para produção; limites bem restritos; disponibilidade sujeita a mudança. |

---

## 4. Como chamar os modelos (Interactions API)

Endpoint base: `POST https://generativelanguage.googleapis.com/v1beta/interactions`
Header opcional de revisão: `Api-Revision: 2026-05-20` (fixa o comportamento da API contra breaking changes).

### 4.1. Parâmetros principais do request

| Campo | Descrição |
|---|---|
| `model` | Nome do modelo (ex.: `gemini-3.6-flash`) |
| `input` | String simples **ou** lista de blocos (`{type, text/data/uri, mime_type}`) |
| `system_instruction` | Instrução de sistema (conta como input tokens) |
| `generation_config` | `temperature`, `thinking_level`, etc. |
| `stream` | `true` = streaming SSE (`?alt=sse` no REST) |
| `previous_interaction_id` | Continua conversa (estado no SERVIDOR — recomendado) |
| `store` | `false` = modo stateless (histórico gerenciado pelo cliente) |
| `tools` | Lista de ferramentas (functions, `google_search`, `code_execution`, etc.) |
| `response_format` | Structured output (`{type:"text", mime_type:"application/json", schema}`) |
| `background` | `true` = execução assíncrona em background (poll via `GET /v1beta/interactions/{id}`) |
| `agent` + `environment` | `agent="antigravity-preview-05-2026"`, `environment="remote"` roda agente gerenciado |

### 4.2. Streaming (SSE)

```bash
curl -X POST ".../v1beta/interactions?alt=sse" \
  -H "x-goog-api-key: $GEMINI_API_KEY" -H 'Content-Type: application/json' \
  --no-buffer -d '{ "model": "gemini-3.6-flash", "input": "Explain how AI works", "stream": true }'
```

Eventos SSE: `interaction.created`, `step.start`, `step.delta` (deltas `text`, `thought_signature`...), `step.stop`, `interaction.completed` (com `usage`). **Erros em streaming** chegam como evento com `"event_type": "error"`.

Python:
```python
stream = client.interactions.create(model="gemini-3.6-flash", input="...", stream=True)
for event in stream:
    if event.event_type == "step.delta" and event.delta.type == "text":
        print(event.delta.text, end="")
```

### 4.3. Multi-turno (conversa)

**Stateful (recomendado)** — o servidor guarda o histórico:
```python
interaction1 = client.interactions.create(model="gemini-3.6-flash", input="I have 2 dogs in my house.")
interaction2 = client.interactions.create(
    model="gemini-3.6-flash",
    input="How many paws are in my house?",
    previous_interaction_id=interaction1.id,
)
```

**Stateless** — `store=False` + você reenvia TODO o histórico (`user_input` + steps do modelo + resultados de função) a cada request. **Obrigatório preservar steps exatamente como recebidos** (signatures de thinking/função são exigidas para continuar).

### 4.4. Multimodal (imagem/áudio/vídeo)

- Via **Files API**: `client.files.upload(file=...)` → usa `uri` do arquivo (vídeo fica `ACTIVE` após processar).
- Via **base64 inline**: bloco `{"type":"image","data":<b64>,"mime_type":"image/jpeg"}`.
- Vídeo/áudio remoto: bloco com `uri` https.

### 4.5. Geração de imagem (Nano Banana)

```python
interaction = client.interactions.create(model="gemini-3.1-flash-image", input="Generate an image of a futuristic city skyline at sunset")
with open("generated_image.png", "wb") as f:
    f.write(base64.b64decode(interaction.output_image.data))
```

### 4.6. Structured output (JSON com schema)

```python
interaction = client.interactions.create(
    model="gemini-3.6-flash",
    input="Give me a recipe for banana bread",
    response_format={"type": "text", "mime_type": "application/json", "schema": Recipe.model_json_schema()},
)
recipe = Recipe.model_validate_json(interaction.output_text)
```
Funciona com Pydantic (Python) e Zod (JavaScript).

### 4.7. Function calling

1. Declare a função em `tools`: `{"type":"function","name":"get_current_temperature","description":"...","parameters":{schema JSON}}`.
2. O modelo devolve step `function_call` com `name`, `arguments` e `id`; status da interação = `requires_action`.
3. Você executa a função localmente e responde com `{"type":"function_result","name":...,"call_id":...,"result":[{"type":"text","text":json}]}` + `previous_interaction_id` (stateful) ou histórico completo (stateless).
4. O modelo retorna a resposta final.

### 4.8. Tools do Google (grounding)

- `tools=[{"type":"google_search"}]` — busca web com citações (`annotations` com `url_citation`).
- Também: `code_execution` (sandbox), `url_context` (URLs públicas), `file_search`, `google_maps`, `computer_use`, `filesystem`, `bash`, `mcp_server`.

### 4.9. Background execution

`background=True` → resposta imediata `status: "in_progress"`; faça poll com `client.interactions.get(id)` (REST: `GET /v1beta/interactions/{id}`) até `completed`/`failed`.

---

## 5. Parâmetros de geração (e faixas válidas)

| Parâmetro | Faixa válida |
|---|---|
| `temperature` | 0.0–1.0 |
| `topP` | 0.0–1.0 |
| `candidateCount` | 1–8 (inteiro) |
| `maxOutputTokens` | ver página de modelos (limite por modelo) |
| `thinking_level` | ex.: `"low"` (controla custo/latência/inteligência) |

> **Atenção**: para modelos Gemini 3.x o Google recomenda FORTEMENTE manter `temperature/top_p/top_k` nos defaults — mudanças podem causar loops ou performance degradada em tarefas de raciocínio/matemática. (Para código/tabelas markdown, temperatura alta >= 0.8 ajuda a evitar repetição.)

> Modelos 2.5 vêm com **thinking ligado por padrão** → maior latência/tokens; desligue/ajuste se quiser velocidade.

---

## 6. Tokens

- **1 token ≈ 4 caracteres**; 100 tokens ≈ 60–80 palavras em inglês.
- **Custo por modalidade**:
  - Imagem: ≤384px em ambas as dimensões = **258 tokens**; imagens maiores são fatiadas em tiles 768×768, cada tile = **258 tokens**.
  - Vídeo: **263 tokens/segundo**.
  - Áudio: **32 tokens/segundo**.
- System instructions e tools **contam como input tokens**.

### 6.1. Como contar tokens
1. **`count_tokens`** (antes de enviar): `client.models.count_tokens(model=..., contents=...)`
   REST: `POST /v1beta/models/gemini-3.6-flash:countTokens`
2. **`usage` na resposta**: `total_input_tokens`, `total_output_tokens`, `total_thought_tokens` (thinking), `total_cached_tokens`, `total_tool_use_tokens`, `total_tokens`.
3. API `GetTokens` existe e **não é cobrada** e **não conta contra cota de inferência**.

---

## 7. ERROS — referência completa

### 7.1. Formato da resposta de erro

Toda a API devolve:
```json
{ "error": { "code": "invalid_request", "message": "..." } }
```
- `code`: string machine-readable em **snake_case** (use no código do app).
- `message`: descrição legível.
- Em **streaming (SSE)**: o erro vem como evento `{"event_type": "error", "error": {"code": ..., "message": ...}}`.
- Código não listado nas tabelas → cai no `snake_case` do HTTP status (fallback).

### 7.2. Códigos padrão de erro (request-level)

| `code` | HTTP | Descrição | Ação recomendada |
|---|---|---|---|
| `invalid_request` | 400 | Payload malformado ou parâmetro inválido | Conferir sintaxe vs API reference |
| `failed_precondition` | 400 | Pré-requisito não atendido (ex.: billing desabilitado) | Verificar billing/status do projeto |
| `out_of_range` | 416 | Parâmetro fora da faixa válida | Ajustar valores |
| `parameter_unknown` | 400 | Parâmetro desconhecido | Remover parâmetro |
| `authentication` | 401 | Chave ausente, inválida ou expirada | Verificar API key |
| `permission_denied` | 403 | Chave sem permissão para o recurso | Verificar permissões da chave/projeto |
| `not_found` | 404 | Recurso não encontrado | Verificar path/parâmetros |
| `model_not_found` | 404 | Modelo não encontrado | Corrigir nome ou usar outro modelo |
| `already_exists` | 409 | Entidade já existe | Checar antes de recriar |
| `aborted` | 409 | Abortado por conflito/concorrência | Retry em nível de aplicação |
| `rate_limit_exceeded` | 429 | Estourou limite por minuto/segundo (RPM/TPM) | Esperar + retry com backoff exponencial |
| `quota_exceeded` | 429 | Estourou a **cota diária** | Esperar reset da cota ou pedir aumento |
| `cancelled` | 499 | Cliente cancelou antes de completar | Nenhuma ação (cliente desconectou) |
| `api_error` | 500 | Erro inesperado no servidor | Retry; persistir → suporte |
| `unimplemented` | 501 | Feature não implementada | Trocar para feature suportada |
| `service_unavailable` | 503 | Serviço sobrecarregado/fora do ar | Retry com backoff exponencial |
| `deadline_exceeded` | 504 | Request não terminou no deadline | Remover/aumentar deadline do cliente |

### 7.3. Códigos de "generation blocked" (política/segurança bloqueou a saída)

Modifique o input e retente:

`safety` · `recitation` (copyright) · `language` (idioma não suportado) · `prohibited_content` · `spii` (PII sensível) · `blocklist` (termos proibidos) · `image_safety` · `image_prohibited_content` · `image_recitation` · `image_other` · `content_blocked`

### 7.4. Códigos de erro de geração (estrutura da saída)

`malformed_function_call` · `malformed_tool_call` · `unexpected_tool_call` (modelo chamou tool não declarada) · `no_image` (não conseguiu gerar imagem) · `too_many_tool_calls` · `missing_thought_signature`

### 7.5. Bloqueios via safety settings (geração)

- `promptFeedback.blockReason` preenchido = **prompt bloqueado**.
- `finishReason == "SAFETY"` = resposta bloqueada; inspecionar `safetyRatings` (categoria + probabilidade).
- `BlockedReason.OTHER` = viola ToS ou não suportado.
- Erro específico de chave vazada: `"Your API key was reported as leaked. Please use another API key."`

### 7.6. Estratégia de retry oficial

- Retry **SOMENTE** em erros transitórios: **429, 408, 5xx**. NUNCA em 400/403 (indicam chave inválida ou sintaxe errada).
- **Backoff exponencial + jitter**: ex. 1s → 2s → 4s → 8s (jitter evita todos os clientes retentarem juntos).
- Máximo de retries definido (evitar loop infinito).
- **SDKs oficiais já fazem isso**: o SDK Python re-tenta automaticamente erros transitórios **até 4 vezes**, delay inicial ~1s e máximo 60s.
- 400/500 → **não cobra tokens**, mas a request **conta contra a cota**.

---

## 8. Rate limits (limites de requisição)

### 8.1. Dimensões
- **RPM** — requests por minuto
- **TPM** — tokens por minuto (input)
- **RPD** — requests por dia (reseta **meia-noite, horário do Pacífico**)
- Modelos específicos podem ter **IPM** (imagens/minuto, Nano Banana) ou **TPD** (tokens/dia).

Regras:
- Exceder **qualquer uma** das dimensões → erro de rate limit (ex.: RPM 20, 21º request no minuto → erro, mesmo sobrando TPM).
- Limites são **por projeto**, não por chave.
- Preview/experimental têm limites **mais restritos**.
- Valores **não são garantidos** — capacidade real pode variar.

### 8.2. Spend-based limits (janela rolante de 10 minutos)

| Tier | Limite de gasto / 10 min |
|---|---|
| Free | N/A |
| Tier 1 | $10 |
| Tier 2 | $200 |
| Tier 3 | $200 |

Estourou → **429 RESOURCE_EXHAUSTED**. Resolver: esperar, reduzir custo dos requests (contexto menor/saída menor) ou pedir aumento.

### 8.3. Tiers (qualificação por conta de billing)

| Tier | Qualificação | Cap de billing |
|---|---|---|
| **Free** | Projeto ativo ou free trial | N/A |
| **Tier 1** | Vincular billing account ativa | $250/mês |
| **Tier 2** | **Pagou $100 + 3 dias** desde o 1º pagamento | $2.000/mês |
| **Tier 3** | **Pagou $1.000 + 30 dias** desde o 1º pagamento | $20.000–$100.000+/mês |

- Upgrade é **automático** (Free→Tier 1 ~instantâneo; demais ~10 min).
- Qualificação conta gasto **cumulativo em TODOS os serviços Google Cloud** da conta de billing (não só Gemini).

### 8.4. Batch API (limites próprios)
- Batch jobs concorrentes: **100**
- Arquivo de input: **2 GB**
- Armazenamento de arquivos: **20 GB**
- Tokens enfileirados por modelo (ex. Tier 1): `gemini-3.6-flash` 3M · `gemini-3.5-flash` 3M · `gemini-3.1-flash-lite` 10M · `gemini-2.5-flash` 3M · `gemini-2.5-pro` 5M · `gemini-2.0-flash` 10M · `gemini-embedding-001` 500k.

### 8.5. Priority inference
Consumo tem limites próprios: default **0.3×** o rate limit standard do modelo/tier.

### 8.6. Onde ver SEUS limites ativos
**AI Studio → página Rate limits** (menu do app). Também: **AI Studio → Dashboard → Usage**. Pedido de aumento: formulário "Request a rate limit increase" (sem garantias).

---

## 9. Onde ver cota, uso e gastos (resumo de telas)

| O que ver | Onde |
|---|---|
| **Uso da API (requests/tokens)** | AI Studio → **Dashboard > Usage** |
| **Rate limits ativos por modelo/tier** | AI Studio → **Rate limits** |
| **Tier do projeto, plano de billing, status** | AI Studio → **Projects** (colunas `Billing Tier` / `Status`) |
| **Saldo de créditos (Prepay), pagamentos, auto-reload** | AI Studio → **Billing** (card "Available credits") |
| **Cap de gasto mensal por projeto** | AI Studio → **Spend** → Monthly spend cap → Edit spend cap |
| **Chaves bloqueadas/vazadas** | AI Studio → **API keys** |
| **Custo histórico no Cloud** | Cloud Console → **Billing → Reports** (agrupar por SKU, filtrar Services = Gemini API) |
| **Custo no Cloud (geral)** | Cloud Console → **Cost management** |

Latências de atualização:
- Débito de créditos do uso: **minutos** (near real-time).
- Upgrade de tier após pagamento: ~**10 min**.
- Gráficos de breakdown de custo total: até **24h**.
- Custo no Cloud Billing Console vs AI Studio: pode divergir por até 24h+.

---

## 10. Billing (cobrança)

### 10.1. Planos (vigentes desde 23/03/2026)
- **Prepay (pré-pago, default para usuários novos)**:
  - Compre créditos com antecedência (**mínimo $10, máximo $5.000**).
  - Uso deduz do saldo em **near real-time**.
  - Créditos **expiram em 12 meses** e **não são reembolsáveis** (exceto ao migrar para Postpay).
  - **Saldo = $0 → TODAS as chaves de TODOS os projetos da conta de billing param na hora.**
  - **Auto-reload**: recarrega automático com gatilho ("saldo abaixo de $X → adiciona $Y") + **limite mensal de auto-carga** (teto para recargas automáticas; compras manuais não contam).
  - Prepay **não cobre** outros serviços Cloud (só Gemini API).
  - Créditos promocionais do Cloud: consumidos **antes** do saldo Prepay (mas você precisa ter saldo Prepay ativo primeiro).
- **Postpay (pós-pago)**:
  - Acumula custos, cobrado no fim do mês ou ao atingir o cap de gasto automático do tier.
  - Migração: **Postpay → Prepay é suportado** (AI Studio → Billing → Switch to Prepay, comprar créditos min $10).
  - **Prepay → Postpay NÃO é suportado** (só no fluxo de upgrade elegível, com reembolso automático do saldo).
- Contas Invoiced (offline) não têm Prepay nem spend caps.

### 10.2. Spend caps (tetos de gasto)
- **Cap por billing account (tier)**: Free N/A · Tier 1 $250 · Tier 2 $2.000 · Tier 3 $20k–$100k+/mês. Atingiu o teto → serviço **pausado para todos os projetos** até o 1º do mês seguinte. (Aumento sob requisição.)
- **Cap por projeto (experimental)**: definível em AI Studio → Spend (roles editor/owner/admin). Overage possível por ~10 min de latência de processamento de billing. Tarefas longas (batch/agents) podem exceder o cap.

### 10.3. Pontos importantes
- **$300 Welcome credit** do Cloud: contas criadas **após 02/03/2026 NÃO** podem usar no Gemini API/AI Studio (só outros produtos Cloud).
- Request que falha com **400 ou 500**: **não cobrada**, mas conta contra cota.
- `GetTokens`: não cobrado, não conta contra cota de inferência.
- O que é cobrado: input tokens, output tokens, cached tokens e armazenamento de cache.
- Free tier: prompts podem ser usados para melhorar produtos Google; Paid tier: **não** são usados.
- Desabilitar billing em um projeto = volta ao Free Tier.
- Saldo negativo (Prepay): possível por latência do sistema; serviço pausa e o negativo é descontado da próxima compra.

---

## 11. Preços (Paid Tier, por 1M tokens, USD)

### Gemini 3.7 Flash (`gemini-3.7-flash`)
| Modalidade | Input | Output (inclui thinking) |
|---|---|---|
| Standard | $0.75* (depois $1.50) | $3.75* (depois $7.50) |
| Batch (50% off) | $0.375* | $1.875* |
| Flex | $0.375* | $1.875* |
| Priority | $1.35* | $6.75* |
| Context caching | $0.075* por 1M tokens + $0.50/1M tokens/hora de armazenamento | |

\* Preço promocional até **31/12/2026**; valores após a barra valem a partir de **01/01/2027**.

### Gemini 3.6 Flash (`gemini-3.6-flash`) — idem ao 3.7 Flash.

### Gemini 3.5 Flash (`gemini-3.5-flash`)
- Standard: input **$1.50** / output **$9.00**
- Batch: **$0.75** / **$4.50**
- Caching: $0.15 + $1.00/1M tokens/h

### Grounding (Google Search / Maps)
- **5.000 requests grátis/mês** (compartilhadas entre todos os modelos Gemini 3.x).
- Depois: **$14 / 1.000 requests** (Search) e **$14 / 1.000 queries** (Maps).
- 1 request do usuário pode gerar 1+ queries ao Google Search — cada query cobrada individualmente.

### Free tier
- Acesso limitado a certos modelos com **input/output grátis** (dentro dos rate limits do free tier).
- Sem Batch/Flex (Batch é recurso pago).
- Conteúdo usado para melhorar produtos Google.

---

## 12. Safety settings (filtros de segurança)

Categorias ajustáveis: **Harassment**, **Hate speech**, **Sexually explicit**, **Dangerous**.
Probabilidades: HIGH / MEDIUM / LOW / NEGLIGIBLE. Bloqueio é por **probabilidade**, não severidade.

| Threshold (AI Studio) | Threshold (API) | Efeito |
|---|---|---|
| Off | `OFF` | Filtro desligado |
| Block none | `BLOCK_NONE` | Mostra sempre |
| Block few | `BLOCK_ONLY_HIGH` | Bloqueia só probabilidade alta |
| Block some | `BLOCK_MEDIUM_AND_ABOVE` | Bloqueia média+ |
| Block most | `BLOCK_LOW_AND_ABOVE` | Bloqueia baixa+ |

- **Default = OFF** para Gemini 2.5 e 3 (filtros adicionais desligados; modelo já tem segurança embutida).
- Danos "core" (ex.: segurança infantil) são **sempre bloqueados, não ajustáveis**.
- Aplicações com filtros MENOS restritivos podem passar por review (ToS).

---

## 13. Troubleshooting (resumo dos guias oficiais)

1. **Retry** — ver seção 7.6.
2. **Parâmetros fora da faixa** — ver seção 5.
3. **Modelo errado / versão errada** — features beta só existem em `/v1beta`; conferir lista de modelos.
4. **Latência alta nos 2.5** — thinking vem ligado por default; desligar/ajustar.
5. **`BlockedReason.OTHER`** — pode violar ToS.
6. **RECITATION** (output parado por similaridade com dados) — deixar prompt/contexto mais único e **subir temperature**.
7. **Repetição de tokens** — temperatura >= 0.8; instruções "Be concise"/"Don't repeat yourself"; para tabelas markdown, instruir separador `|---|---|`; para structured output, não definir ordem de campos e tornar todos obrigatórios; evitar escapes `\u` no prompt.
8. **Chave bloqueada por vazamento** — gerar nova no AI Studio; erro retornado: "Your API key was reported as leaked...".
9. **Idiomas não suportados** — ver lista de idiomas disponíveis.
10. **Fórum oficial** para bugs: Google AI developer forum.

---

## 14. Deprecações e ciclo de vida (resumo atual)

| Modelo | Data de shutdown (mais cedo) | Substituir por |
|---|---|---|
| `gemini-3.1-flash-lite` | 07/05/2027 | `gemini-3.5-flash-lite` |
| `gemini-3-pro-preview` | 09/03/2026 (já) | `gemini-3.1-pro-preview` |
| `gemini-3.1-flash-lite-preview` | 25/05/2026 (já) | `gemini-3.1-flash-lite` |
| `gemini-3.1-flash-image-preview` | 25/06/2026 (já) | `gemini-3.1-flash-image` |
| `gemini-3-pro-image-preview` | 25/06/2026 (já) | `gemini-3-pro-image` |
| `gemini-2.5-pro-preview-*` | 02/12/2025 (já) | `gemini-3.1-pro-preview` |
| `gemini-2.0-flash` / `-lite` | **Shut down** | `gemini-2.5-flash` / `-lite` |

Regras: deprecação = anúncio de fim de suporte; shutdown = endpoint desligado de vez. Datas na tabela = **mínimo** possível; o Google comunica a data exata com antecedência. Acompanhar em Release notes e na página de deprecations.

---

## 15. Outros recursos relevantes da API

- **OpenAI compatibility**: endpoint compatível com a API da OpenAI para migração fácil (`/v1beta/openai/`).
- **Context caching**: cache de contexto pago (~10% do preço de input) para reduzir custo em prompts repetidos.
- **Batch API**: jobs em lote com **50% de desconto** (pago), limites próprios.
- **Flex inference**: throughput com desconto, menor prioridade (batch-like, mas interativo).
- **Priority inference**: maior prioridade, ~1.8× preço.
- **Live API**: áudio/vídeo bidirecional em tempo real (WebSockets; ephemeral tokens).
- **Files API**: upload de arquivos para input multimodal.
- **Agents**: Antigravity (sandbox remoto com código/arquivos/web), custom agents, Deep Research.
- **Logs e datasets**: logging de requests no AI Studio; política de compartilhamento de dados configurável.
- **Regiões**: ver página "Available regions"; free tier disponível em EEA/UK/CH e muitas outras.
- **Webhooks** para batch/background; **tokens efêmeros** para Live API.
- **Abuse monitoring**: uso é monitorado contra abuso (política pública).

---

## 16. Links oficiais

| Assunto | URL |
|---|---|
| Docs (início) | https://ai.google.dev/gemini-api/docs |
| Get started (tutorial) | https://ai.google.dev/gemini-api/docs/get-started |
| API keys | https://ai.google.dev/gemini-api/docs/api-key |
| Models | https://ai.google.dev/gemini-api/docs/models |
| Text generation | https://ai.google.dev/gemini-api/docs/text-generation |
| Streaming | https://ai.google.dev/gemini-api/docs/streaming |
| Structured output | https://ai.google.dev/gemini-api/docs/structured-output |
| Function calling | https://ai.google.dev/gemini-api/docs/function-calling |
| Tools | https://ai.google.dev/gemini-api/docs/tools |
| Agents | https://ai.google.dev/gemini-api/docs/agents |
| Batch API | https://ai.google.dev/gemini-api/docs/batch-api |
| Context caching | https://ai.google.dev/gemini-api/docs/caching |
| Tokens | https://ai.google.dev/gemini-api/docs/tokens |
| **API errors** | https://ai.google.dev/gemini-api/docs/api-errors |
| **Rate limits** | https://ai.google.dev/gemini-api/docs/rate-limits |
| **Billing** | https://ai.google.dev/gemini-api/docs/billing |
| Pricing | https://ai.google.dev/gemini-api/docs/pricing |
| Troubleshooting | https://ai.google.dev/gemini-api/docs/troubleshooting |
| Safety settings | https://ai.google.dev/gemini-api/docs/safety-settings |
| Deprecations | https://ai.google.dev/gemini-api/docs/deprecations |
| Release notes | https://ai.google.dev/gemini-api/docs/changelog |
| OpenAI compatibility | https://ai.google.dev/gemini-api/docs/openai |
| Available regions | https://ai.google.dev/gemini-api/docs/available-regions |
| Abuse monitoring | https://ai.google.dev/gemini-api/docs/usage-policies |
| AI Studio (chaves/uso/billing) | https://aistudio.google.com |

---

## 17. Resumo prático (cheat sheet)

```bash
# 1. Gerar texto
curl -X POST "https://generativelanguage.googleapis.com/v1beta/interactions" \
  -H "x-goog-api-key: $GEMINI_API_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"gemini-3.6-flash","input":"Olá"}'

# 2. Streaming
curl --no-buffer -X POST ".../v1beta/interactions?alt=sse" \
  -H "x-goog-api-key: $GEMINI_API_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"gemini-3.6-flash","input":"Olá","stream":true}'

# 3. Contar tokens antes de enviar
curl -X POST ".../v1beta/models/gemini-3.6-flash:countTokens" \
  -H "x-goog-api-key: $GEMINI_API_KEY" -H 'Content-Type: application/json' \
  -d '{"contents":[{"parts":[{"text":"meu texto"}]}]}'

# 4. Ver uso/cota → AI Studio > Dashboard > Usage e AI Studio > Rate limits
# 5. Ver gastos/saldo → AI Studio > Billing (e Spend p/ caps) / Cloud Console > Billing Reports
```

**Regra de ouro para erros**: `code` snake_case no corpo → trate programaticamente; 429/408/5xx → backoff exponencial + jitter; 400/401/403 → NUNCA retry cego (corrigir causa).
