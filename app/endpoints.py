import json
import threading
import os
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import PASTA
from . import audio as audio_mod
from . import config as config_mod
from . import db, proxy
from .janelas import JANELAS, limites_por_chave

try:
    import diagnostico
except ImportError:  # execucao a partir de fora da raiz: garante o contrato no path
    import sys

    sys.path.insert(0, str(PASTA))
    import diagnostico

router = APIRouter()

NOMES_JANELAS = {"minuto": "RPM/TPM (min)", "dia": "RPD (dia)", "mes": "Mês (tokens)"}


class NovaChave(BaseModel):
    nome: str
    key: str
    tipo: str = "gemini"


class AlterarChave(BaseModel):
    ativa: bool


class PedidoAudio(BaseModel):
    model: str = ""
    input: str = ""
    voice: str = "Kore"
    response_format: str = "wav"


def _cfg(request):
    return request.app.state.cfg


def _estado(request):
    return request.app.state.estado


def _agendador(request):
    return request.app.state.agendador


def _token_ok(request, cfg):
    token = (cfg.get("gateway_token") or "").strip()
    if not token:
        return True
    auth = request.headers.get("authorization", "")
    return auth == f"Bearer {token}"


def _cota_publica(cfg):
    """Fato medido no banco (2026-09-07): o que derruba pedidos de
    gemini-3.8-flash e a janela de MINUTO (RPM/TPM por chave). Dia e mes
    existem mas nunca estouraram (pico real: 1% do dia, 22% do mes; todo
    'Quota exceeded' do Google dizia limit:20 ou limit:250000 com
    'retry in Xs' <= 60s). Exposto em todo /v1 para que qualquer
    consumidor da API saiba a janela real sem precisar perguntar."""
    lim = cfg.get("limites") or {}
    n = max(0, len(config_mod.chaves_ativas(cfg)))
    rpm = int(lim.get("rpm") or 0)
    tpm = int(lim.get("tpm") or 0)
    return {
        "janela_efetiva": "minuto",
        "rpm_por_chave": rpm,
        "tpm_por_chave": tpm,
        "requisicoes_dia_por_chave": int(lim.get("requisicoes_dia") or 0),
        "tokens_mes_por_chave": int(lim.get("tokens_mes") or 0),
        "chaves_ativas": n,
        "pool_rpm": rpm * n,
        "pool_tpm": tpm * n,
    }


def _headers_cota(cfg):
    c = _cota_publica(cfg)
    return {
        "x-ratelimit-limit-requests": str(c["rpm_por_chave"]),
        "x-ratelimit-limit-tokens": str(c["tpm_por_chave"]),
        "x-ratelimit-window": "60s",
        "x-ratelimit-limit-requests-dia": str(c["requisicoes_dia_por_chave"]),
        "x-ratelimit-limit-tokens-mes": str(c["tokens_mes_por_chave"]),
        "x-gateway-chaves-ativas": str(c["chaves_ativas"]),
        "x-gateway-pool-rpm": str(c["pool_rpm"]),
        "x-gateway-pool-tpm": str(c["pool_tpm"]),
    }


def _sem_chave(erro, cfg=None):
    mensagem = str(erro)
    retry_after = erro.retry_after
    headers = _headers_cota(cfg) if cfg else {}
    detalhe = {"message": mensagem}
    if cfg:
        detalhe["cota"] = _cota_publica(cfg)
    if retry_after:
        headers["Retry-After"] = str(retry_after)
        resp = JSONResponse(
            {"error": {**detalhe, "retry_after": retry_after}},
            status_code=429,
            headers=headers,
        )
    else:
        resp = JSONResponse({"error": detalhe}, status_code=503, headers=headers)
    # Rejeicoes geradas pelo gateway que NUNCA chegaram ao Google (invisiveis
    # no requisicoes): pacing de onda e queda de rede. Registrar para poder
    # diagnosticar sem adivinhar.
    if "onda:" in mensagem or "rede indisponivel" in mensagem:
        try:
            db.registrar_honesto(mensagem, retry_after, 0)
        except Exception:
            pass
    return resp


def _segundos_ate(iso):
    if not iso:
        return None
    try:
        fim = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if fim.tzinfo is None:
            fim = fim.replace(tzinfo=timezone.utc)
        return max(0, int((fim - datetime.now(timezone.utc)).total_seconds()))
    except ValueError:
        return None


@router.get("/", include_in_schema=False)
def indice():
    return FileResponse(PASTA / "site" / "index.html")


@router.get("/api/saude")
def saude():
    return {"ok": True, "versao": "1.0", "hora": datetime.now().isoformat(timespec="seconds")}


# --------------------------------------------------------------------------
# Saude REAL (componentes). O /api/saude acima NAO muda: e o contrato do
# watchdog do launcher e nao pode reagir a estados degradados-mas-servindo
# (ex.: banco somente-leitura), sob pena de transformar problema mole em
# kill/reload. A riqueza diagnostica vive aqui e no heartbeat.
# --------------------------------------------------------------------------

_detalhe_lock = threading.Lock()
_detalhe_tick_anterior = None
_detalhe_ok_anterior = None


def medir_componentes(app_state, tick_anterior):
    """Fotografia honesta dos componentes, calculada do MESMO jeito pelo
    heartbeat (thread do main) e por /api/saude/detalhe. event_loop_ok exige
    comparacao com a leitura anterior: so o contador de ticks do loop prova
    que o loop esta girando (socket vivo nao prova — uvicorn aceita conexao
    mesmo com o loop congelado). Retorna (componentes, ticks_atuais)."""
    ticks = int(getattr(app_state, "_loop_ticks", 0))
    if tick_anterior is None:
        event_loop_ok = ticks > 0
    else:
        event_loop_ok = ticks > tick_anterior
    agendador = getattr(app_state, "agendador", None)
    try:
        poller_vivo = bool(agendador and agendador.vivo())
    except Exception:
        poller_vivo = False
    try:
        db_escrevivel = db.escrevivel()
    except Exception:
        db_escrevivel = False
    try:
        chaves = len(config_mod.chaves_ativas(app_state.cfg))
    except Exception:
        chaves = 0
    racers = None
    try:
        est = getattr(app_state, "estado", None)
        if est is not None:
            racers = {
                "alvo_ma": round(est.racers_alvo(int(getattr(app_state, "cfg", {}).get("racers_janela", 10))), 2),
                "pedidos_em_voo": est.pedidos_em_voo,
            }
    except Exception:
        racers = None
    return {
        "event_loop_ok": event_loop_ok,
        "poller_vivo": poller_vivo,
        "db_escrevivel": db_escrevivel,
        "chaves_ativas": chaves,
        "loop_ticks": ticks,
        "racers": racers,
    }, ticks


@router.get("/api/saude/detalhe")
def saude_detalhe(request: Request):
    global _detalhe_tick_anterior, _detalhe_ok_anterior
    try:
        with _detalhe_lock:
            componentes, ticks = medir_componentes(request.app.state, _detalhe_tick_anterior)
            _detalhe_tick_anterior = ticks
            ok = bool(componentes["event_loop_ok"] and componentes["poller_vivo"] and componentes["db_escrevivel"])
            if ok != _detalhe_ok_anterior:  # so na mudanca de estado: sem spam no JSONL
                _detalhe_ok_anterior = ok
                try:
                    diagnostico.emitir("saude_real", "servidor", ok=ok, componentes=componentes)
                except Exception:
                    pass
        return {"ok": ok, "componentes": componentes, "ts": datetime.now().isoformat(timespec="seconds")}
    except Exception as erro:  # sensor nunca derruba a request path
        return {
            "ok": True,
            "componentes": {"erro": str(erro)},
            "ts": datetime.now().isoformat(timespec="seconds"),
        }


_INICIO_PROCESSO = time.time()


def _deploy_id():
    """Impressao digital do codigo em execucao: maior mtime dos .py do app.

    Fecha a lacuna "harness roda codigo novo, producao roda codigo velho":
    compare este valor com o disco para saber se o deploy esta atualizado.
    """
    try:
        pasta = PASTA / "app"
        mais_novo = 0.0
        for nome in os.listdir(pasta):
            if nome.endswith(".py"):
                try:
                    mais_novo = max(mais_novo, os.path.getmtime(pasta / nome))
                except OSError:
                    pass
        return datetime.fromtimestamp(mais_novo).isoformat(timespec="seconds") if mais_novo else None
    except Exception:
        return None


@router.get("/api/versao_codigo")
def versao_codigo():
    return {
        "ok": True,
        "deploy_id": _deploy_id(),
        "pid": os.getpid(),
        "iniciado_em": datetime.fromtimestamp(_INICIO_PROCESSO).isoformat(timespec="seconds"),
    }


def _status_chave(snap, limiar, recent):
    """Estado real da chave: cruza o estado vivo (erro/cooldown/quota) com o
    historico recente de requisicoes do banco. Assim uma chave que esta sendo
    massacrada por 429 aparece como ERRO/CRITICO mesmo depois de o cooldown
    transitório zerar.
    """
    if snap["invalida"]:
        return "invalida"
    if snap.get("bloqueio_tipo") == "quota":
        return "esgotada"
    falhas = (recent or {}).get("falhas", 0)
    ok = (recent or {}).get("ok", 0)
    if snap["erro"] or (snap.get("cooldown_restante") or 0) > 0:
        return "erro"
    # 1-2 falhas isoladas sao ruido (ex.: um 400 do cliente); 3+ em 10min
    # indicam chave instavel de verdade (massacre de 429 etc.)
    if falhas >= 3:
        if falhas >= ok:
            return "critico"
        return "erro"
    maior = snap.get("maior_percent")
    if maior is not None and maior >= limiar:
        return "alerta"
    return "ok"


@router.get("/api/chaves")
def chaves(request: Request):
    cfg = _cfg(request)
    estado = _estado(request)
    recentes = db.balanco_recente_por_chave(600)
    limiar = float(cfg.get("limiar_alerta", 80))
    dados = []
    for chave in cfg["chaves"]:
        snap = estado.snapshot_chave(chave["nome"])
        status = _status_chave(snap, limiar, recentes.get(chave["nome"]))
        dados.append(
            {
                "nome": chave["nome"],
                "tipo": chave["tipo"],
                "key_mascarada": config_mod.mascarar(chave["key"]),
                "ativa": chave.get("ativa") is not False,
                "status": status,
                "em_voo": snap["em_voo"],
                "cooldown_restante": snap["cooldown_restante"],
                "bloqueio_tipo": snap.get("bloqueio_tipo"),
                "bloqueado_ate": snap.get("bloqueado_ate"),
                "erro": snap["erro"],
                "falhas_10m": recentes.get(chave["nome"], {}).get("falhas", 0),
                "ok_10m": recentes.get(chave["nome"], {}).get("ok", 0),
                "atualizado_em": snap["atualizado_em"],
            }
        )
    return {"total": len(dados), "chaves": dados}


@router.get("/api/resumo")
def resumo(request: Request):
    cfg = _cfg(request)
    estado = _estado(request)
    hoje = db.requisicoes_por_chave_hoje()
    recentes = db.balanco_recente_por_chave(600)
    uso_dia = db.uso_dia_ok_por_chave()
    limiar = float(cfg.get("limiar_alerta", 80))
    itens = []
    for chave in cfg["chaves"]:
        nome = chave["nome"]
        snap = estado.snapshot_chave(nome)
        snap["maior_percent"] = None
        janelas = snap["janelas"] or {}
        jan_out = {}
        maior = None
        for jan_nome in JANELAS:
            jan = janelas.get(jan_nome) or {}
            pct = jan.get("percent")
            jan_out[jan_nome] = {
                "percent": pct,
                "resetsAt": jan.get("resetsAt"),
                "segundos_ate_reset": _segundos_ate(jan.get("resetsAt")),
            }
            if pct is not None:
                maior = float(pct) if maior is None else max(maior, float(pct))
        snap["maior_percent"] = maior
        recent = recentes.get(nome)
        status = _status_chave(snap, limiar, recent)
        itens.append(
            {
                "nome": nome,
                "tipo": chave["tipo"],
                "ativa": chave.get("ativa") is not False,
                "status": status,
                "maior_percent": maior,
                "limiar": limiar,
                "janelas": jan_out,
                "dia_reqs": uso_dia.get(nome, 0),
                "dia_limite": limites_por_chave(cfg, chave).get("requisicoes_dia"),
                "em_voo": snap["em_voo"],
                "cooldown_restante": snap["cooldown_restante"],
                "bloqueio_tipo": snap.get("bloqueio_tipo"),
                "bloqueado_ate": snap.get("bloqueado_ate"),
                "erro": snap["erro"],
                "atualizado_em": snap["atualizado_em"],
                "requisicoes_hoje": hoje.get(nome),
                "falhas_10m": (recent or {}).get("falhas", 0),
                "ok_10m": (recent or {}).get("ok", 0),
            }
        )
    return {"hora": datetime.now().isoformat(timespec="seconds"), "chaves": itens}


@router.get("/api/uso")
def uso(request: Request, chave: str = None, limite: int = 200):
    limite = max(1, min(1000, limite))
    return {"snapshots": db.historico_uso(chave=chave, limite=limite)}


@router.get("/api/requisicoes")
def requisicoes(request: Request, limite: int = 100):
    limite = max(1, min(1000, limite))
    return {"requisicoes": db.historico_requisicoes(limite=limite)}


@router.get("/api/estatisticas")
def estatisticas(request: Request):
    return db.estatisticas()


@router.get("/api/configuracao")
def configuracao(request: Request):
    cfg = _cfg(request)
    return {
        "porta": cfg.get("porta"),
        "endpoint_gateway": f"http://127.0.0.1:{cfg.get('porta', 8787)}/v1",
        "intervalo_uso_seg": cfg.get("intervalo_uso_seg"),
        "limiar_alerta": cfg.get("limiar_alerta"),
        "max_conc_por_key": cfg.get("max_conc_por_key"),
        "cooldown_429_seg": cfg.get("cooldown_429_seg"),
        "cooldown_429_max_seg": cfg.get("cooldown_429_max_seg"),
        "cooldown_5xx_seg": cfg.get("cooldown_5xx_seg"),
        "max_ciclos_espera": cfg.get("max_ciclos_espera"),
        "espera_cooldown_max_seg": cfg.get("espera_cooldown_max_seg"),
        "chaves_por_tentativa": cfg.get("chaves_por_tentativa"),
        "racers_cap_total": cfg.get("racers_cap_total"),
        "racers_janela": cfg.get("racers_janela"),
        "timeout_http_seg": cfg.get("timeout_http_seg"),
        "gateway_token_definido": bool((cfg.get("gateway_token") or "").strip()),
        "limites": cfg.get("limites"),
        "modelos": cfg.get("modelos"),
        "chaves": [
            c["nome"] if c.get("ativa") is not False else f'{c["nome"]} (desativada)' for c in cfg["chaves"]
        ],
    }


@router.post("/api/atualizar")
def atualizar(request: Request):
    resultado = _agendador(request).atualizar_tudo()
    return {"ok": True, "resultado": resultado}


@router.post("/api/chaves")
def adicionar_chave(request: Request, corpo: NovaChave):
    cfg = _cfg(request)
    nome = (corpo.nome or "").strip()
    key = (corpo.key or "").strip()
    if not nome:
        return JSONResponse({"erro": "nome vazio"}, status_code=400)
    if not key or "COLE-SUA-KEY" in key:
        return JSONResponse({"erro": "key vazia ou placeholder"}, status_code=400)
    if any(c["nome"] == nome for c in cfg["chaves"]):
        return JSONResponse({"erro": f"ja existe uma chave chamada {nome}"}, status_code=409)
    tipo = (corpo.tipo or "gemini").strip().lower() or "gemini"
    nova = {"nome": nome, "key": key, "tipo": tipo, "ativa": True}
    cfg["chaves"].append(nova)
    config_mod.salvar(cfg)
    threading.Thread(target=_agendador(request).atualizar_chave, args=(nova,), daemon=True).start()
    return {
        "ok": True,
        "chave": {"nome": nome, "tipo": tipo, "ativa": True, "key_mascarada": config_mod.mascarar(key)},
    }


@router.delete("/api/chaves/{nome}")
def excluir_chave(nome: str, request: Request):
    cfg = _cfg(request)
    antes = len(cfg["chaves"])
    cfg["chaves"] = [c for c in cfg["chaves"] if c["nome"] != nome]
    if len(cfg["chaves"]) == antes:
        return JSONResponse({"erro": "chave nao encontrada"}, status_code=404)
    config_mod.salvar(cfg)
    _estado(request).esquecer(nome)
    return {"ok": True, "nome": nome}


@router.put("/api/chaves/{nome}")
def alterar_chave(nome: str, corpo: AlterarChave, request: Request):
    cfg = _cfg(request)
    alvo = next((c for c in cfg["chaves"] if c["nome"] == nome), None)
    if alvo is None:
        return JSONResponse({"erro": "chave nao encontrada"}, status_code=404)
    alvo["ativa"] = bool(corpo.ativa)
    config_mod.salvar(cfg)
    _estado(request).esquecer(nome)
    return {"ok": True, "nome": nome, "ativa": alvo["ativa"]}


@router.get("/v1/models")
def modelos(request: Request):
    cfg = _cfg(request)
    if not _token_ok(request, cfg):
        return JSONResponse({"error": {"message": "token do gateway invalido"}}, status_code=401)
    return JSONResponse(
        {
            "object": "list",
            "data": [{"id": m, "object": "model", "owned_by": "gemini-gateway"} for m in cfg["modelos"]],
            "cota": _cota_publica(cfg),
        },
        headers=_headers_cota(cfg),
    )


@router.post("/api/modelos/sincronizar")
def sincronizar_modelos(request: Request):
    from . import modelos as mod_modelos

    resultado = mod_modelos.sincronizar(_cfg(request))
    if not resultado.get("ok"):
        return JSONResponse({"erro": resultado.get("motivo", "falhou")}, status_code=400)
    return {"ok": True, "resultado": resultado, "modelos": _cfg(request)["modelos"]}


def _resposta_upstream(erro, cfg=None):
    """Erro do Google repassado HONESTO: status real + corpo real. Se o
    corpo nao chegou (transporte), o gateway sintetiza com a descricao —
    nunca status sem corpo: erro mudo e o que nao pode existir."""
    corpo = erro.corpo
    if corpo:
        try:
            json.loads(corpo)
        except ValueError:
            corpo = None
    if not corpo:
        corpo = json.dumps({"error": {"message": erro.desc}}).encode("utf-8")
    return Response(
        corpo,
        status_code=erro.status,
        media_type="application/json",
        headers=_headers_cota(cfg) if cfg else None,
    )


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    cfg = _cfg(request)
    estado = _estado(request)
    if not _token_ok(request, cfg):
        return JSONResponse({"error": {"message": "token do gateway invalido"}}, status_code=401)
    corpo = await request.body()

    # Validacao na borda: modelo inexistente nunca deve virar 404 do Google
    # (queima cota e chega mudo). O gateway conhece sua propria lista.
    modelo = proxy.campo_json(corpo, "model", "")
    if not str(modelo or "").strip():
        return JSONResponse({"error": {"message": "campo 'model' vazio ou ausente"}}, status_code=400)
    lista = cfg.get("modelos") or []
    if lista and modelo not in lista:
        return JSONResponse(
            {"error": {"message": f"modelo '{modelo}' nao existe no gateway; a lista oficial e GET /v1/models"}},
            status_code=404,
        )

    if proxy.pedido_e_stream(corpo):
        try:
            ok, resultado = await run_in_threadpool(proxy.abrir_stream, estado, cfg, corpo)
        except proxy.SemChaveDisponivel as erro:
            return _sem_chave(erro, cfg)
        except proxy.ErroUpstream as erro:
            return _resposta_upstream(erro, cfg)
        if not ok:
            return JSONResponse({"error": {"message": "falha no proxy"}}, status_code=502)
        resp, nome, req_id = resultado

        def gerador():
            tin = tout = 0
            try:
                for linha in resp:
                    proxy.registrar_assinatura_de_chunk(linha)
                    yield linha
                    tin, tout = proxy.tokens_de_linha(linha, tin, tout)
            except Exception as erro:
                estado.marcar_erro(nome, f"stream interrompido: {erro}")
            finally:
                proxy.descartar_stream(resp)
                if tin or tout:
                    try:
                        db.atualizar_requisicao(req_id, tokens_in=tin, tokens_out=tout)
                    except Exception:
                        pass
                    estado.registrar_uso_local(nome, tin + tout)
                estado.liberar(nome, getattr(resp, "_reserva_tokens", 0))
                estado.decrementar_em_voo(nome)

        return StreamingResponse(
            gerador(), status_code=resp.status, media_type="text/event-stream",
            headers=_headers_cota(cfg),
        )

    try:
        ok, resultado = await run_in_threadpool(proxy.enviar_completo, estado, cfg, corpo)
    except proxy.SemChaveDisponivel as erro:
        return _sem_chave(erro, cfg)
    except proxy.ErroUpstream as erro:
        return _resposta_upstream(erro, cfg)
    if not ok:
        return JSONResponse({"error": {"message": resultado}}, status_code=502)
    status, corpo_resp, _nome = resultado
    return Response(
        corpo_resp, status_code=status, media_type="application/json",
        headers=_headers_cota(cfg),
    )


@router.post("/v1/audio/speech")
async def audio_speech(pedido: PedidoAudio, request: Request):
    """TTS (padrao OpenAI): {"model": "...-tts...", "input": "texto", "voice": "Kore"}.

    O chat em /v1/chat/completions continua igual; isto e so um adicional.
    """
    cfg = _cfg(request)
    estado = _estado(request)
    if not _token_ok(request, cfg):
        return JSONResponse({"error": {"message": "token do gateway invalido"}}, status_code=401)
    modelo = (pedido.model or "").strip()
    texto = (pedido.input or "").strip()
    if not modelo:
        return JSONResponse({"error": {"message": "campo 'model' vazio"}}, status_code=400)
    if not texto:
        return JSONResponse({"error": {"message": "campo 'input' vazio"}}, status_code=400)
    if not audio_mod.e_modelo_tts(modelo):
        tts = [m for m in (cfg.get("modelos") or []) if audio_mod.e_modelo_tts(m)]
        dica = f" Use um modelo TTS: {', '.join(tts)}" if tts else " Sincronize os modelos no painel."
        return JSONResponse(
            {"error": {"message": f"modelo '{modelo}' nao e TTS.{dica}"}}, status_code=400
        )
    if modelo not in (cfg.get("modelos") or []):
        return JSONResponse(
            {"error": {"message": f"modelo '{modelo}' nao esta na lista do gateway (/v1/models)"}},
            status_code=404,
        )
    try:
        audio, content_type, _nome = await run_in_threadpool(
            audio_mod.gerar, estado, cfg, modelo, texto,
            pedido.voice or "Kore", pedido.response_format or "wav",
        )
    except ValueError as erro:
        return JSONResponse({"error": {"message": str(erro)}}, status_code=400)
    except audio_mod.SemChaveDisponivel as erro:
        if erro.retry_after is None:
            return JSONResponse({"error": {"message": str(erro)}}, status_code=400)
        return _sem_chave(erro, cfg)
    return Response(audio, media_type=content_type)
