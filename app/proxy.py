import json
import random
import threading
import time
import urllib.error

from . import config, db, gemini_api
from .janelas import limites_por_chave, reset_dia_ts, reset_mes_ts
from .onda import obter_scheduler as _obter_onda
from .roteador import LIMITE_ESGOTADA, escolher_n

try:
    import diagnostico
except ImportError:  # fora da raiz: garante o contrato no path
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
    import diagnostico


def _trilha(evento, **detalhe):
    """Um pedido do cliente, contado do inicio ao fim no log de diagnostico.

    Um JSONL por decisao (inicio/rodada/falha/espera/fim) em
    logs/eventos-diagnostico.log. So metadados — nunca corpo, nunca segredo
    de chave. diagnostico nunca levanta; o try aqui e cinto duplo para a
    trilha jamais interferir no caminho do pedido.
    """
    try:
        diagnostico.emitir(evento, "proxy", **detalhe)
    except Exception:
        pass

CODES_REPASSE = (400, 404, 416, 422)
CODES_INVALIDAS = (401, 403)
CODES_COOLDOWN = (408, 429)

_modelos_lock = threading.Lock()


class SemChaveDisponivel(Exception):
    def __init__(self, mensagem, retry_after=None):
        super().__init__(mensagem)
        self.retry_after = retry_after


class ErroUpstream(Exception):
    """O Google respondeu um erro do cliente (400/404/416/422): o corpo faz
    parte da resposta e NUNCA pode se perder. Nao e uma resposta — e um erro
    com tipo proprio, levantado, nunca retornado: assim nenhum caminho pode
    confundir um erro com um stream iteravel (foi assim que o 400/404
    chegava mudo ao cliente: um objeto-erro polimorfico viajava como se
    fosse resposta e o gerador de stream nao sabia iteralo)."""

    def __init__(self, status, corpo, nome, req_id, desc):
        super().__init__(desc)
        self.status = status
        self.corpo = corpo
        self.nome = nome
        self.req_id = req_id
        self.desc = desc


def aprender_modelo_morto(cfg, modelo):
    """404 do Google num modelo que estava na lista: a listagem mentiu
    (deprecado mas ainda aparece em /v1beta/models). O gateway aprende a
    verdade e a persiste — o modelo sai da roteacao e nunca mais gera 404.
    Substitui a lista manual de manutengao: o mecanismo e o proprio trafego."""
    if not modelo:
        return
    with _modelos_lock:
        atuais = list(cfg.get("modelos") or [])
        if modelo not in atuais:
            return
        atuais.remove(modelo)
        cfg["modelos"] = atuais
        mortos = list(cfg.get("modelos_indisponiveis") or [])
        if modelo not in mortos:
            mortos.append(modelo)
        cfg["modelos_indisponiveis"] = mortos
        try:
            config.salvar(cfg)
        except OSError:
            pass


def _fmt_duracao(seg):
    seg = int(seg)
    h, m = divmod(seg // 60, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m {seg % 60:02d}s"


def _hora_local_daqui_a(seg):
    from datetime import datetime, timedelta

    alvo = datetime.now() + timedelta(seconds=seg)
    return alvo.strftime("%H:%M")


def campo_json(corpo_bytes, campo, padrao=None):
    try:
        dados = json.loads(corpo_bytes)
        return dados.get(campo, padrao) if isinstance(dados, dict) else padrao
    except ValueError:
        return padrao


def _gravar_assinatura(call_id, sig):
    if not call_id or not sig:
        return
    db.gravar_assinatura(call_id, sig)


def _buscar_assinatura(call_id):
    return db.ler_assinatura(call_id)


def registrar_assinaturas_de_resposta(corpo_bytes):
    try:
        dados = json.loads(corpo_bytes)
        mensagem = (dados.get("choices") or [{}])[0].get("message") or {}
    except (ValueError, TypeError, IndexError, AttributeError):
        return
    for tc in mensagem.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        sig = (tc.get("extra_content") or {}).get("google", {}).get("thought_signature")
        _gravar_assinatura(tc.get("id"), sig)


def registrar_assinatura_de_chunk(chunk_bytes):
    try:
        linha = chunk_bytes.decode("utf-8", "ignore").strip()
        if not linha.startswith("data:"):
            return
        dados = json.loads(linha[5:].strip())
    except (ValueError, TypeError):
        return
    for tc in (dados.get("choices") or [{}])[0].get("delta", {}).get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        sig = (tc.get("extra_content") or {}).get("google", {}).get("thought_signature")
        _gravar_assinatura(tc.get("id"), sig)


def injetar_assinaturas(corpo_bytes):
    try:
        dados = json.loads(corpo_bytes)
        if not isinstance(dados, dict):
            return corpo_bytes
    except ValueError:
        return corpo_bytes
    mudou = False
    for msg in dados.get("messages") or []:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            if not isinstance(tc, dict) or not tc.get("id"):
                continue
            extra = tc.get("extra_content")
            if isinstance(extra, dict) and (extra.get("google") or {}).get("thought_signature"):
                continue
            sig = _buscar_assinatura(tc["id"])
            if not sig:
                continue
            tc["extra_content"] = {"google": {"thought_signature": sig}}
            mudou = True
    if not mudou:
        return corpo_bytes
    return json.dumps(dados).encode("utf-8")


def _corpo_com_esforco(corpo_bytes, esforco):
    if not esforco:
        return corpo_bytes
    try:
        dados = json.loads(corpo_bytes)
    except ValueError:
        return corpo_bytes
    if not isinstance(dados, dict):
        return corpo_bytes
    modelo = (dados.get("model") or "").lower()
    if not modelo.startswith("gemini-"):
        return corpo_bytes
    if dados.get("reasoning_effort"):
        return corpo_bytes
    dados["reasoning_effort"] = esforco
    return json.dumps(dados).encode("utf-8")


def _corpo_com_max_tokens(corpo_bytes, max_tokens):
    if not max_tokens:
        return corpo_bytes
    try:
        dados = json.loads(corpo_bytes)
    except ValueError:
        return corpo_bytes
    if not isinstance(dados, dict):
        return corpo_bytes
    modelo = (dados.get("model") or "").lower()
    if not modelo.startswith("gemini-"):
        return corpo_bytes
    atual = dados.get("max_tokens")
    if isinstance(atual, int) and atual >= max_tokens:
        return corpo_bytes
    dados["max_tokens"] = max_tokens
    return json.dumps(dados).encode("utf-8")


def _corpo_com_fechamento(corpo_bytes):
    # O Gemini (OpenAI-compat) rejeita pedidos que terminam em um turno do
    # modelo/assistente ("Requests ending with a model turn are not supported").
    # O opencode as vezes reenvia o historico com a ultima fala do assistente no
    # fim (stream interrompido, retry, compactacao). Para fechar a conversa,
    # acrescentamos um turno final de usuario.
    try:
        dados = json.loads(corpo_bytes)
    except ValueError:
        return corpo_bytes
    if not isinstance(dados, dict):
        return corpo_bytes
    msgs = dados.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return corpo_bytes
    ultima = msgs[-1]
    if not isinstance(ultima, dict):
        return corpo_bytes
    papel = str(ultima.get("role") or "").strip().lower()
    if papel not in ("assistant", "model"):
        return corpo_bytes
    msgs.append({"role": "user", "content": "Continue."})
    return json.dumps(dados).encode("utf-8")


def pedido_e_stream(corpo_bytes):
    return campo_json(corpo_bytes, "stream") is True


def _tokens_de_uso(uso, tin_padrao=0, tout_padrao=0):
    if not isinstance(uso, dict) or not uso:
        return tin_padrao, tout_padrao
    tin = int(uso.get("prompt_tokens") or tin_padrao)
    tout = int(uso.get("completion_tokens") or tout_padrao)
    total = int(uso.get("total_tokens") or 0)
    if total > 0 and total >= tin + tout:
        # o Gemini nao inclui tokens de raciocinio em completion_tokens,
        # mas cobra em total_tokens: saida real = total - entrada.
        tout = total - tin
    return tin, tout


def tokens_do(corpo_bytes):
    try:
        dados = json.loads(corpo_bytes)
    except (ValueError, TypeError):
        return 0, 0
    if not isinstance(dados, dict):
        return 0, 0
    return _tokens_de_uso(dados.get("usage"))


def com_stream_usage(corpo_bytes):
    try:
        dados = json.loads(corpo_bytes)
    except (ValueError, TypeError):
        return corpo_bytes
    if not isinstance(dados, dict):
        return corpo_bytes
    opcoes = dados.get("stream_options")
    if not isinstance(opcoes, dict):
        opcoes = {}
    opcoes["include_usage"] = True
    dados["stream_options"] = opcoes
    return json.dumps(dados).encode("utf-8")


def tokens_de_linha(linha, tin, tout):
    texto = linha.decode("utf-8", "ignore").strip()
    if not texto.startswith("data:"):
        return tin, tout
    try:
        dados = json.loads(texto[5:].strip())
    except ValueError:
        return tin, tout
    uso = dados.get("usage") if isinstance(dados, dict) else None
    return _tokens_de_uso(uso, tin, tout)


def _retry_after(headers):
    try:
        valor = int(headers.get("Retry-After") or headers.get("retry-after") or 0)
    except (TypeError, ValueError):
        return None
    return valor if 0 < valor <= 86400 else None


def _registrar_descartada(estado, nome, modelo, resp, inicio, reserva=0):
    """Uma corrida pode ter varios vencedores: a resposta que perdeu tambem
    foi processada pelo Google e consumiu cota. Ler e registrar, senao o
    contador local fica menor que o uso real e a chave 'estoura' sem aviso."""
    ms = int((time.time() - inicio) * 1000)
    tin = tout = 0
    corpo_ok = False
    try:
        corpo = resp.read()
        tin, tout = tokens_do(corpo)
        registrar_assinaturas_de_resposta(corpo)
        corpo_ok = True
    except Exception:
        pass
    try:
        db.registrar_requisicao(
            nome, modelo, True,
            status=getattr(resp, "status", None), ms=ms,
            tokens_in=tin, tokens_out=tout,
            erro=None if corpo_ok else "descartada (corpo ilegivel)",
        )
    except Exception:
        pass
    try:
        estado.registrar_uso_local(nome, tin + tout)
    except Exception:
        pass
    estado.liberar(nome, reserva)
    if corpo_ok:
        gemini_api.devolver(resp)  # corpo lido: conexao limpa, volta ao pool
    else:
        gemini_api.descartar(resp)


def estimar_tokens(corpo_bytes):
    """Estimativa de tokens de ENTRADA a partir do corpo do pedido.

    Calibrada com dados reais do historico: o tokenizer do Google roda a
    ~3 chars por token no conteudo do opencode (codigo/JSON); chars/4
    subestimava ~30% e deixava passar pedidos que estouravam o balde de
    250k do Google (o 429 confessava: input_token_count limit: 250000).
    Extrair so o texto evita superestimar pedidos pequenos (moldura JSON).
    Fallback bytes/3 para corpo ilegivel (lado seguro: por cima). Piso de 1.
    """
    try:
        dados = json.loads(corpo_bytes)
    except (ValueError, TypeError):
        return max(1, len(corpo_bytes or b"") // 3)

    def _texto_de(valor):
        if isinstance(valor, str):
            return valor
        if isinstance(valor, list):
            return "".join(_texto_de(item) for item in valor)
        if isinstance(valor, dict):
            return "".join(_texto_de(v) for v in valor.values())
        return ""

    msgs = dados.get("messages")
    texto = _texto_de(msgs) if isinstance(msgs, list) else ""
    return max(1, len(texto) // 3)


def _abrir(estado, cfg, corpo_bytes, stream=False):
    """Casca do pedido para os racers adaptativos: marca o agente como em voo
    (e isso divide o budget da frota entre agentes concorrentes) e registra o
    sinal do pedido — chaves queimadas ate o vencedor + 1 — quando ele termina,
    por QUALQUER saida (sucesso, erro do Google, desistencia honesta).

    Gera o req_id que amarra a trilha do pedido no log de diagnostico
    (pedido_inicio/rodada/falha/espera/fim): o dia inteiro fica gravado e
    legivel pedido a pedido.
    """
    cap = max(1, int(cfg.get("racers_cap_total", 6)))
    medidor = {"falhas": 0, "rodadas": 0}
    medidor["req"] = "%x%04x" % (int(time.time() * 1000) % 16**8, random.randrange(16**4))
    _t0 = time.time()
    _modelo = ""
    try:
        _modelo = campo_json(corpo_bytes, "model", "")
    except Exception:
        pass
    estado.pedido_iniciar()
    try:
        try:
            _res = _abrir_interno(estado, cfg, corpo_bytes, medidor)
            _trilha("pedido_fim", req=medidor["req"], saida="ok",
                    status_cliente=200,
                    ms_total=int((time.time() - _t0) * 1000),
                    chaves_queimadas=medidor["falhas"],
                    modelo=_modelo, stream=stream)
            return _res
        except SemChaveDisponivel as erro:
            _trilha("pedido_fim", req=medidor["req"], saida="sem_chave",
                    status_cliente=429 if erro.retry_after else 503,
                    ms_total=int((time.time() - _t0) * 1000),
                    chaves_queimadas=medidor["falhas"],
                    retry_after=erro.retry_after, detalhe=str(erro)[:200])
            raise
        except ErroUpstream as erro:
            _trilha("pedido_fim", req=medidor["req"], saida="erro_google",
                    status_cliente=erro.status,
                    ms_total=int((time.time() - _t0) * 1000),
                    chaves_queimadas=medidor["falhas"],
                    detalhe=str(erro.desc if hasattr(erro, "desc") else erro)[:200])
            raise
        finally:
            estado.registrar_sinal_racers(min(medidor["falhas"] + 1, cap))
    finally:
        estado.pedido_finalizar()


def _abrir_interno(estado, cfg, corpo_bytes, medidor):
    modelo = campo_json(corpo_bytes, "model", "")
    corpo_bytes = injetar_assinaturas(corpo_bytes)
    corpo_bytes = _corpo_com_fechamento(corpo_bytes)
    corpo_bytes = _corpo_com_esforco(corpo_bytes, str(cfg.get("reasoning_effort") or "").strip())
    corpo_bytes = _corpo_com_max_tokens(corpo_bytes, int(cfg.get("max_tokens_padrao") or 0))
    inicio = time.time()
    timeout = int(cfg.get("timeout_http_seg", 120))
    cooldown_base = int(cfg.get("cooldown_429_seg", 15))
    teto_429 = int(cfg.get("cooldown_429_max_seg", 300))
    cooldown_5xx = int(cfg.get("cooldown_5xx_seg", 10))
    espera_max = int(cfg.get("espera_cooldown_max_seg", 30))
    n_racers = max(1, int(cfg.get("chaves_por_tentativa", 3)))
    max_ciclos = max(1, int(cfg.get("max_ciclos_espera", 3)))
    tentativas_max = config.tentativas_por_pedido(cfg)
    ciclos = 0
    deadline = inicio + int(cfg.get("max_espera_requisicao_seg", 120))
    usadas = set()
    motivos = []
    limites_rpm = {c["nome"]: int(limites_por_chave(cfg, c).get("rpm") or 0) for c in cfg.get("chaves") or []}
    limites_tpm = {c["nome"]: int(limites_por_chave(cfg, c).get("tpm") or 0) for c in cfg.get("chaves") or []}
    # Cota compartilhada por projeto (docs/DIAGNOSTICO_COTA.md, secao 7-F1):
    # todas as keys do mesmo projeto Google compartilham UM balde de RPM/TPM.
    # A valvula passa a ser UM balde de fichas da frota inteira (lambda <= mu
    # no POOL, nao por chave) e o estado passa a somar uso/reservas do grupo
    # na admissao preditiva. Um 429 de quota trava o grupo (F2), nao queima
    # a pool: queimar 36 chaves num balde morto so gasta mais RPM do projeto.
    _cota_compartilhada = bool(cfg.get("cota_compartilhada"))
    _nomes_frota = [nome_k for nome_k in limites_rpm]
    if _cota_compartilhada and _nomes_frota:
        _rpm_grupo = max([v for v in limites_rpm.values()] or [0])
        _teto_grupo = max([v for v in limites_tpm.values()] or [0])
        estado.valvula.configurar_grupo(_nomes_frota, _rpm_grupo)
        estado.ativar_cota_grupo(_nomes_frota, _teto_grupo)
    else:
        for nome_k, rpm_k in limites_rpm.items():
            estado.valvula.configurar(nome_k, rpm_k)
    estado.configurar_teto_reserva(limites_tpm)
    estimativa = estimar_tokens(corpo_bytes)

    # --- WaveScheduler: pacing de grandes por minuto-calendario, POR CHAVE ---
    # Kill-switch default-off: quando desabilitado (ou pedido pequeno), apenas
    # deve_agendar() e segue byte-identico, sem tocar usadas/tentativas/ciclos.
    # O pacing acontece em dois pontos: (1) eleicao exclui chaves com o minuto
    # ocupado; (2) se NADA elegivel restar e o bloqueio for lease, estaciona
    # ate a proxima :00 (orcamento separado) ou rejeita com honestidade.
    _onda = None
    _onda_deve = False
    try:
        _onda = _obter_onda(estado, cfg)
        _onda_deve = bool(_onda.deve_agendar(estimativa)) if _onda is not None else False
    except Exception:
        _onda = None
        _onda_deve = False
    _onda_gasto = 0.0

    # --- Racers adaptativos: budget de frota por media movel (substitui o hedge) ---
    # 503/hang e loteria de capacidade do lado do Google (prova de 2026-09-07:
    # nenhuma diferenca mensuravel entre a chave que acertou e as que falharam
    # — todas com 0 req/min no minuto da tempestade). Nao da para prever,
    # entao aposta-se em paralelo. Sinal de cada pedido = chaves queimadas ate
    # o vencedor; a media movel da janela e o budget TOTAL da frota, dividido
    # pelos agentes em voo (N agentes nao disparam N*cap de uma vez). Bonanca:
    # alvo ~1, serial, zero desperdicio. Tempestade: sobe ate o cap — e a
    # corrida e barata justamente quando importa, pois 503 nao consome TPM (a
    # reserva e liberada; o perdedor gasta so o slot RPM que o failover serial
    # gastaria de qualquer jeito, porem em serie). Dentro do pedido, cada
    # rodada que falha soma +1 na seguinte: a PRIMEIRA vitima da tempestade ja
    # se beneficia, sem esperar a media dos proximos pedidos. O episodio
    # continua vivo apenas para o corte de timeout de hang (abaixo).
    _racers_cap = max(1, int(cfg.get("racers_cap_total", 6)))
    _racers_janela = max(1, int(cfg.get("racers_janela", 10)))
    _racers_base = max(n_racers, estado.racers_para_pedido(_racers_cap, _racers_janela))
    try:
        _alvo_ma = round(estado.racers_alvo(_racers_janela), 2)
    except Exception:
        _alvo_ma = None
    _trilha("pedido_inicio", req=medidor.get("req"), modelo=modelo,
            tokens_est=estimativa, racers_base=_racers_base, alvo_ma=_alvo_ma)
    try:
        _ep_janela = int(cfg.get("episodio_janela_seg", 300))
        _ep_min503 = int(cfg.get("episodio_min_503", 2))
        _ep_minch = int(cfg.get("episodio_min_chaves", 2))
    except (TypeError, ValueError):
        _ep_janela, _ep_min503, _ep_minch = 300, 2, 2
    # Timeout adaptativo por episodio (uma fonte de episodio p/ hedge e corte):
    # fora de episodio o teto e o timeout_http_seg (nunca mata sucesso lento);
    # em episodio, TTFB acima de timeout_episodio_seg vira falha rapida e o
    # pedido re-tenta em outra chave em vez de pendurar 50s+ no hang.
    try:
        _teto_episodio = int(cfg.get("timeout_episodio_seg", 30))
    except (TypeError, ValueError):
        _teto_episodio = 30

    def rpm_cheia(nome):
        limite = limites_rpm.get(nome, 0)
        if limite <= 0:
            return False
        if _cota_compartilhada:
            # balde do projeto: soma a frota inteira, nao so esta chave
            reqs, _ = estado.uso_60s_grupo()
        else:
            reqs, _ = estado.uso_60s(nome)
        return reqs >= limite

    def tpm_cheia(nome):
        limite = limites_tpm.get(nome, 0)
        if limite <= 0:
            return False
        if _cota_compartilhada:
            # balde do projeto: soma a frota inteira, nao so esta chave
            _, tokens = estado.uso_60s_grupo()
        else:
            _, tokens = estado.uso_60s(nome)
        return tokens >= limite

    def _onda_ocupada(nome):
        # Exclusao por lease de minuto (fail-open: nunca quebrar a eleicao).
        if not _onda_deve or _onda is None:
            return False
        try:
            return bool(_onda.minuto_ocupado(nome))
        except Exception:
            return False

    # Orcamento proprio p/ falha de REDE (DNS/conexao): nao come as tentativas
    # da pool. Erro de transporte e GRATIS (nao consome cota nem
    # pendura o Google), entao VALE insistir por um janela longa o bastante para
    # cobrir o "acordar do link" (medido: ~30s num dongle Wi-Fi suspenso). A
    # janela e por TEMPO (tentativas_rede_seg) com backoff exponencial+jitter;
    # tentativas_rede e so um teto de seguranca. Persistiu alem da janela ->
    # falha honesto ("rede indisponivel"), nunca "teto de tentativas" mentiroso.
    rede_tentativas = 0
    rede_max = int(cfg.get("tentativas_rede", 8))
    rede_budget_seg = max(1.0, float(cfg.get("tentativas_rede_seg", 45)))
    rede_ate = None
    # Estacionamento em tempestade de capacidade: teto de ciclos curtos para
    # nao virar "thinking forever"; o deadline continua sendo o limite duro.
    _park_ciclos = 0
    _park_max = max(1, int(cfg.get("park_ciclos_max", 6)))

    while True:
        if time.time() > deadline:
            raise SemChaveDisponivel(
                "; ".join(motivos) if motivos else "tempo maximo de espera esgotado", 60
            )
        if len(usadas) >= tentativas_max:
            # ESTACIONAR antes de desistir: se o bloqueio e de CAPACIDADE
            # (medidor viu 503/429-generico/timeout), as chaves estao em
            # cooldown curto e vao liberar juntas em segundos. Andar as 36 e
            # depois levantar "teto(36)" era o que vazava 429 ao cliente numa
            # tempestade passageira. Vale esperar a proxima vaga enquanto
            # houver deadline — o teto vira ultimo recurso, nao primeira saida.
            _espera_vaga = estado.menor_retry_restante()
            if (
                medidor["falhas"] > 0
                and _espera_vaga is not None
                and _espera_vaga > 0
                and time.time() + _espera_vaga + 1 < deadline
                and _park_ciclos < _park_max
            ):
                _park_ciclos += 1
                _espera = min(_espera_vaga + 0.1, max(0.1, deadline - time.time()))
                _trilha("pedido_espera", req=medidor.get("req"), segundos=round(_espera, 1),
                        motivo="teto_pool_capacidade", ciclo=_park_ciclos)
                time.sleep(_espera)
                usadas.clear()
                continue
            restante = estado.menor_retry_restante() or estado.menor_quota_reset()
            retry = int(restante) + 2 if restante is not None and restante > 0 else int(cfg.get("cooldown_429_seg", 15))
            raise SemChaveDisponivel(
                "teto de tentativas atingido ("
                + str(tentativas_max)
                + "): "
                + ("; ".join(motivos) if motivos else "sem chave livre"),
                retry,
            )
        # Timeout da rodada: em episodio, TTFB acima do teto vira falha rapida
        # (failover em segundos em vez de hang de 50s+); fora dele, o teto
        # normal nunca corta sucesso lento. Reavaliado por rodada porque o
        # episodio pode comecar no meio deste pedido.
        try:
            _em_episodio = bool(estado.episodio_ativo(_ep_janela, _ep_min503, _ep_minch))
        except Exception:
            _em_episodio = False
        if _em_episodio and _teto_episodio > 0:
            timeout = _teto_episodio
        else:
            timeout = int(cfg.get("timeout_http_seg", 120))
        # Racers da rodada: alvo compartilhado (MA10 / agentes em voo) + o
        # reflexo intra-pedido (cada rodada que falhou por capacidade soma +1),
        # sempre respeitando o cap da frota.
        _n = min(_racers_cap, max(n_racers, _racers_base + medidor["rodadas"]))
        if _cota_compartilhada and _n > 2:
            # balde unico do projeto: racers fazem sentido entre baldes
            # INDEPENDENTES; com cota compartilhada, paralelismo so multiplica
            # o RPM do mesmo projeto morto. Teto 2: hedge de capacidade leve.
            _n = 2
        # Guarda da frota: quando a MAIORIA das chaves esta doente (tempestade
        # generalizada), abrir leque de 6 so multiplica o volume que o Google ja
        # esta recusando (auto-RPM) e queima cota. Ai o leque encolhe para 2:
        # ainda aposta casado (acha vencedor se houver), sem martelar a pool.
        if _n > 2:
            try:
                _doentes = sum(1 for _nm in _nomes_frota if estado.erros_10min(_nm))
                if _nomes_frota and _doentes * 2 > len(_nomes_frota):
                    _n = 2
            except Exception:
                pass
        candidatas, razao = escolher_n(
            estado, cfg, _n,
            extra_cheia=lambda nome: rpm_cheia(nome) or tpm_cheia(nome) or _onda_ocupada(nome),
            tokens_necessarios=estimativa,
        )
        _trilha("pedido_rodada", req=medidor.get("req"), rodada=medidor["rodadas"] + 1,
                n_racers=_n, chaves=[c.get("nome") for c in candidatas],
                razao=str(razao)[:200])
        candidatas = [c for c in candidatas if c["nome"] not in usadas]
        if candidatas:
            com_ficha, espera_valvula = [], None
            for c in list(candidatas):
                ok_ficha, t_ficha = estado.valvula.consumir(c["nome"])
                if ok_ficha:
                    com_ficha.append(c)
                elif espera_valvula is None or t_ficha < espera_valvula:
                    espera_valvula = t_ficha
            if com_ficha:
                candidatas = com_ficha
            else:
                # nenhuma vaga agora: esperar a PROXIMA vaga exata (finita) ou
                # devolver 429 com Retry-After calculado — nunca fila infinita
                if (
                    espera_valvula is not None
                    and 0 <= espera_valvula <= espera_max
                    and ciclos < max_ciclos
                    and time.time() <= deadline
                ):
                    ciclos += 1
                    _trilha("pedido_espera", req=medidor.get("req"),
                            segundos=round(espera_valvula + 0.05, 1),
                            motivo="valvula_rpm", ciclo=ciclos)
                    time.sleep(espera_valvula + 0.05)
                    continue
                raise SemChaveDisponivel(
                    "todas as chaves estao na cota de RPM; proxima vaga em "
                    f"{_fmt_duracao(espera_valvula or 0)}",
                    int(espera_valvula or 0) + 2,
                )
        if not candidatas:
            if _onda is not None:
                # Estacionar SEMPRE que o bloqueio for capacidade de balde
                # (lease de minuto OU folga TPM zerada) — a virada :00 zera
                # ambos. Independente do limiar de pacing: um pedido abaixo do
                # threshold ainda deve ESPERAR a vaga certa, nao virar erro.
                # Re-elege ignorando folga/lease, mantendo os bloqueios duros
                # (cooldown/cota/RPM/valvula/percent): se ainda vazio, segue o
                # fluxo existente. Nao consome usadas/tentativas/ciclos.
                try:
                    _alt, _ = escolher_n(
                        estado, cfg, _n,
                        extra_cheia=lambda nome: rpm_cheia(nome) or tpm_cheia(nome),
                        tokens_necessarios=0,
                    )
                    _alt = [c for c in _alt if c["nome"] not in usadas]
                except Exception:
                    _alt = []
                # Nao filtra por teto aqui: pedido oversized (>teto) tambem pode
                # estacionar — na virada :00 os baldes ficam pristinos e a
                # eleicao admite 1 oversized por balde vazio. Filtrar por teto
                # recriaria o deadlock de admissao (nenhuma chave "cabe").
                if _alt:
                    try:
                        _max_onda = float(_onda.max_espera_seg())
                    except Exception:
                        _max_onda = 30.0
                    try:
                        _espera = float(_onda.tempo_para_proxima_onda())
                    except Exception:
                        _espera = 0.0
                    if _espera <= 0:
                        # fronteira ja virou entre eleicao e calculo: re-elege
                        time.sleep(0.02)
                        continue
                    _agora3 = time.time()
                    if _onda_gasto + _espera > _max_onda or _agora3 + _espera > deadline:
                        _r = max(1, int(_espera) + 1)
                        raise SemChaveDisponivel(
                            f"onda: espera ate a proxima onda excede o orcamento; tente apos {_r}s", _r)
                    _entrou = False
                    try:
                        try:
                            _entrou = bool(_onda.entrar_fila())
                        except Exception:
                            _entrou = True
                        if not _entrou:
                            _r2 = max(1, int(_espera) + 1)
                            raise SemChaveDisponivel(
                                f"onda: fila cheia; tente apos {_r2}s", _r2)
                        _rest = _espera
                        while _rest > 0:
                            if time.time() > deadline:
                                raise SemChaveDisponivel(
                                    "onda: deadline insuficiente para a proxima onda",
                                    max(1, int(_rest) + 1))
                            _fatia = min(2.0, _rest)
                            if _fatia <= 0:
                                break
                            time.sleep(_fatia)
                            _rest -= _fatia
                            _onda_gasto += _fatia
                    finally:
                        if _entrou:
                            try:
                                _onda.sair_fila()
                            except Exception:
                                pass
                    continue
            restante = estado.quando_libera(cfg["chaves"], LIMITE_ESGOTADA)
            if restante is not None and 0 < restante <= espera_max and ciclos < max_ciclos:
                ciclos += 1
                _trilha("pedido_espera", req=medidor.get("req"),
                        segundos=round(restante + 0.05, 1),
                        motivo="cooldown_pool", ciclo=ciclos)
                time.sleep(restante + 0.05)
                usadas.clear()
                continue
            quota_reset = estado.menor_quota_reset()
            if quota_reset is not None:
                msg = (
                    "cota esgotada em todas as chaves; reseta em "
                    f"{_fmt_duracao(quota_reset)} (as {_hora_local_daqui_a(quota_reset)})"
                )
                retry_after = min(int(quota_reset) + 1, 3600)
            else:
                # esvaziou por janela de RPM: calcular a vaga exata em vez de
                # devolver um Retry-After generico
                rpm_restante = estado.quando_libera_rpm()
                if rpm_restante is not None and 0 < rpm_restante <= teto_429:
                    msg = (
                        "todas as chaves estao na cota de RPM; proxima vaga em "
                        + _fmt_duracao(rpm_restante)
                    )
                    retry_after = int(rpm_restante) + 2
                else:
                    msg = "; ".join(motivos) if motivos else razao
                    # se o bloqueio e de RPM/TPM, avisar o cliente exatamente
                    # quando a proxima chave abre, em vez de um "60s" generico
                    retry_restante = estado.menor_retry_restante()
                    if retry_restante is not None and 0 < retry_restante <= teto_429:
                        retry_after = int(retry_restante) + 2
                    else:
                        retry_after = 60
            raise SemChaveDisponivel(msg, retry_after)

        prontas = []
        for c in candidatas:
            # Reserva os tokens ANTES de disparar; se outro pedido em paralelo
            # ocupou a folga primeiro (corrida), este candidato e descartado e
            # o laco re-roteia para as demais chaves.
            if not estado.reservar(c["nome"], estimativa):
                continue
            # Claim atomico do lease de minuto (pacing por chave). Se outra
            # thread ocupou esta chave neste minuto, solta a reserva e a ficha
            # da valvula e re-elege — sem queimar usadas/tentativas.
            if _onda_deve and _onda is not None:
                try:
                    _lease_ok = bool(_onda.reivindicar(c["nome"]))
                except Exception:
                    _lease_ok = True
                if not _lease_ok:
                    estado.liberar(c["nome"], estimativa)
                    try:
                        estado.valvula.devolver(c["nome"])
                    except Exception:
                        pass
                    continue
            usadas.add(c["nome"])
            estado.incrementar_em_voo(c["nome"])
            prontas.append(c)
            # MODELO DA CAIXA: a chave disparada vai para o FUNDO da fila
            # global no ato (todos os agentes compartilham a mesma fila).
            try:
                estado.rotacionar_fila(c["nome"])
            except Exception:
                pass
        candidatas = prontas
        if not candidatas:
            time.sleep(0.05)
            continue

        caixa = {"resp": None, "nome": None, "falhas": []}
        lock = threading.Lock()
        sucesso_evt = threading.Event()

        def tentar(chave):
            try:
                resp = gemini_api.abrir_chat(chave["key"], corpo_bytes, timeout=timeout,
                                             nome=chave["nome"])
                with lock:
                    if caixa["resp"] is None:
                        caixa["resp"] = resp
                        caixa["nome"] = chave["nome"]
                        sucesso_evt.set()
                    else:
                        _registrar_descartada(estado, chave["nome"], modelo, resp, inicio, estimativa)
            except gemini_api.RedeIndisponivel as erro:
                # Nao alcancamos o Google: a chave nao tem culpa. Classe
                # separada para o laco NAO andar de chave nem cooldown.
                with lock:
                    caixa["falhas"].append((chave["nome"], "rede", erro, None, str(erro)))
            except urllib.error.HTTPError as erro:
                corpo_erro = erro.read()
                desc = gemini_api.descricao_erro(erro.code, corpo_erro)
                with lock:
                    caixa["falhas"].append((chave["nome"], "http", erro, corpo_erro, desc))
                    # 503 OU 429 sem metrica de quota = recusa de capacidade
                    # (nao e culpa da chave): e o sinal do budget adaptativo.
                    if erro.code == 503 or (
                        erro.code == 429 and gemini_api.quarenta_e_nove_de_capacidade(corpo_erro)
                    ):
                        medidor["falhas"] += 1
            except Exception as erro:
                # http.client.HTTPException e similares nao sao OSError: sem este
                # guarda o em_voo/reserva da chave vazava e ela ficava ocupada.
                with lock:
                    caixa["falhas"].append((chave["nome"], "net", erro, None, str(erro)))
                    # Hang/timeout de leitura: o Google nao entregou e nem
                    # devolveu 503 — conta igual como falha de capacidade.
                    medidor["falhas"] += 1

        _f0 = medidor["falhas"]  # snapshot p/ saber se ESTA rodada queimou chave
        threads = [threading.Thread(target=tentar, args=(c,), daemon=True) for c in candidatas]
        for t in threads:
            t.start()

        prazo_tentativa = time.time() + timeout + 5
        while not sucesso_evt.is_set() and time.time() < prazo_tentativa:
            if all(not t.is_alive() for t in threads):
                break
            time.sleep(0.02)

        if caixa["resp"] is not None:
            caixa["resp"]._reserva_tokens = estimativa
            nome_ok = caixa["nome"]
            ms = int((time.time() - inicio) * 1000)
            try:
                req_id = db.registrar_requisicao(nome_ok, modelo, True, status=caixa["resp"].status, ms=ms)
            except Exception:
                req_id = None
            estado.marcar_sucesso(nome_ok)
            for c in candidatas:
                if c["nome"] != nome_ok:
                    estado.decrementar_em_voo(c["nome"])
            return caixa["resp"], nome_ok, req_id

        for t in threads:
            t.join(timeout + 5)

        repasse = None
        houve_rede = False
        ultimo_rede = None
        for nome, tipo, erro, corpo_erro, desc in caixa["falhas"]:
            estado.decrementar_em_voo(nome)
            estado.liberar(nome, estimativa)
            estado.valvula.devolver(nome)
            if tipo == "rede":
                # Nao alcancamos o Google: a chave nao e culpada. Devolve o
                # lease de minuto (nenhuma cota foi gasta), tira do usadas
                # (nao come tentativas da pool), SEM cooldown e SEM marcar_erro.
                usadas.discard(nome)
                if _onda is not None:
                    try:
                        _onda.liberar_minuto(nome)
                    except Exception:
                        pass
                houve_rede = True
                ultimo_rede = str(erro)
                continue
            ms = int((time.time() - inicio) * 1000)
            # Trilha do pedido: o que cada tentativa encontrou e o que o
            # gateway decidiu com isso (cooldown aplicado + estado do balde
            # da chave NA HORA — e a prova de "por que rate limit com 36 chaves").
            _cd_s = 0.0
            _ev_tipo = "transporte"
            _ev_quota = False
            _ev_retry = None
            if tipo == "http":
                if erro.code in CODES_REPASSE:
                    req_id = db.registrar_requisicao(nome, modelo, False, status=erro.code, ms=ms, erro=desc)
                    if erro.code == 404:
                        aprender_modelo_morto(cfg, modelo)
                    repasse = (erro.code, corpo_erro, nome, req_id, desc)
                    _trilha("pedido_falha", req=medidor.get("req"), chave=nome,
                            tipo="repasse_google", status=erro.code,
                            erro_google=str(desc)[:200])
                    continue
                db.registrar_requisicao(nome, modelo, False, status=erro.code, ms=ms, erro=desc)
                if erro.code in CODES_INVALIDAS:
                    estado.marcar_invalida(nome)
                    _ev_tipo = "chave_invalida"
                elif erro.code == 429:
                    if gemini_api.quarenta_e_nove_de_capacidade(corpo_erro):
                        # 429 generico SEM a metrica de quota = o Google dizendo
                        # "modelo lotado" com outra roupa (medido 2026-09-07).
                        # Trata como o 503 que ele e: cooldown curto, alimenta o
                        # episodio/fallback, NAO espelha retry de RPM.
                        estado.marcar_retry(
                            nome, f"capacidade (HTTP 429): {desc}", time.time() + cooldown_5xx
                        )
                        _cd_s = float(cooldown_5xx)
                        _ev_tipo = "capacidade"
                        try:
                            estado.registrar_503(nome)
                        except Exception:
                            pass
                    else:
                        _ev_quota = True
                        _ev_tipo = "quota"
                        try:
                            _ev_retry = gemini_api.retry_delay_seg(corpo_erro)
                        except Exception:
                            _ev_retry = None
                        alvo = gemini_api.quota_reset_alvo(corpo_erro)
                        if alvo == "mes":
                            _quota_ate = reset_mes_ts()
                            _quota_tipo = "quota"
                            _quota_msg = "cota mensal esgotada: " + desc
                            estado.marcar_quota(nome, _quota_msg, _quota_ate)
                            _cd_s = max(0.0, _quota_ate - time.time())
                        elif alvo == "dia":
                            _quota_ate = reset_dia_ts()
                            _quota_tipo = "quota"
                            _quota_msg = "cota diaria esgotada: " + desc
                            estado.marcar_quota(nome, _quota_msg, _quota_ate)
                            _cd_s = max(0.0, _quota_ate - time.time())
                        else:
                            # Espelho do Google: esperar EXATAMENTE o que ele pediu
                            # (retryDelay/Retry-After), sem multiplicador inventado.
                            retry = (
                                gemini_api.retry_delay_seg(corpo_erro)
                                or _retry_after(erro.headers)
                                or cooldown_base
                            )
                            retry = min(retry, teto_429)
                            _quota_ate = time.time() + retry
                            _quota_tipo = "retry"
                            _quota_msg = "rate limit (RPM/TPM): " + desc
                            estado.marcar_retry(nome, _quota_msg, _quota_ate)
                            _cd_s = float(retry)
                        if _cota_compartilhada:
                            # F2 - cooldown de GRUPO: sob cota por projeto, um
                            # 429 de quota significa que o balde COMUM estourou.
                            # Trocar de chave e inutil (mesmo balde) e so queima
                            # mais RPM do projeto: trava a frota inteira pelo
                            # mesmo prazo que o Google pediu, e o pedido espera
                            # a vaga (park) ou falha rapido com Retry-After
                            # honesto - em vez de queimar 36 chaves em rodadas.
                            try:
                                estado.marcar_cooldown_grupo(
                                    _quota_msg + f" [via {nome}; cota do projeto e compartilhada]",
                                    _quota_ate,
                                    _quota_tipo,
                                )
                            except Exception:
                                pass
                elif erro.code in CODES_COOLDOWN or erro.code >= 500:
                    # 5xx/408 ("high demand" etc.): descanso curto e fixo em vez
                    # de re-martelar a chave no mesmo segundo. O 503 alimenta
                    # tambem a memoria de frota (hedge condicionado a episodio).
                    estado.marcar_retry(
                        nome, f"indisponivel (HTTP {erro.code}): {desc}", time.time() + cooldown_5xx
                    )
                    _cd_s = float(cooldown_5xx)
                    _ev_tipo = "capacidade"
                    if erro.code == 503:
                        try:
                            estado.registrar_503(nome)
                        except Exception:
                            pass
                else:
                    estado.marcar_erro(nome, desc)
                    _ev_tipo = "erro"
                motivos.append(f"{nome}: {desc}")
                try:
                    _r60, _t60 = estado.uso_60s(nome)
                except Exception:
                    _r60, _t60 = 0, 0
                _trilha("pedido_falha", req=medidor.get("req"), chave=nome,
                        tipo=_ev_tipo, status=erro.code if tipo == "http" else None,
                        erro_google=str(desc if tipo == "http" else erro)[:200],
                        cooldown_s=round(_cd_s, 1), reqs_60s=_r60, tokens_60s=_t60,
                        quota_real=_ev_quota, retry_google_s=_ev_retry)
            else:
                db.registrar_requisicao(nome, modelo, False, ms=ms, erro=str(erro))
                estado.marcar_erro(nome, str(erro))
                motivos.append(f"{nome}: {erro}")

        # Esta rodada queimou chaves por capacidade? A proxima aposta +1
        # (reflexo intra-pedido, limitado pelo cap da frota no topo do laco).
        if medidor["falhas"] > _f0:
            medidor["rodadas"] += 1

        if repasse is not None:
            status, corpo_erro, nome, req_id, desc = repasse
            raise ErroUpstream(status, corpo_erro, nome, req_id, desc)

        if houve_rede:
            # Rede caiu (DNS/conexao), nao a chave: segura o pedido e insiste
            # (single-flight do DNS resolve de novo) por uma janela de TEMPO,
            # sem andar de chave e sem queimar as tentativas da pool. O link suspenso
            # acorda em ~30s; desistir em 3s era o que te mostrava erro.
            rede_tentativas += 1
            if rede_ate is None:
                rede_ate = time.time() + rede_budget_seg
            if rede_tentativas > rede_max or time.time() >= rede_ate:
                raise SemChaveDisponivel(
                    f"rede indisponivel (DNS/conexao): {ultimo_rede}; aguardando a rede",
                    5,
                )
            # backoff exponencial (1,2,4,8...) com jitter +-25%, limitado pela
            # janela restante e pelo deadline global (nunca dorme alem do prazo).
            passo = min(8.0, 1.0 * (2 ** (rede_tentativas - 1)))
            passo *= 0.75 + random.random() * 0.5
            teto = min(rede_ate - time.time(), deadline - time.time())
            time.sleep(max(0.2, min(passo, teto)))
            continue
    raise SemChaveDisponivel("; ".join(motivos) if motivos else "sem chave disponivel", 60)


def enviar_completo(estado, cfg, corpo_bytes):
    resp, nome, req_id = _abrir(estado, cfg, corpo_bytes)
    try:
        corpo = resp.read()
        tin, tout = tokens_do(corpo)
        registrar_assinaturas_de_resposta(corpo)
        try:
            if req_id is not None:
                db.atualizar_requisicao(req_id, tokens_in=tin, tokens_out=tout)
        except Exception:
            pass
        estado.registrar_uso_local(nome, tin + tout)
        estado.liberar(nome, getattr(resp, "_reserva_tokens", 0))
        estado.decrementar_em_voo(nome)
        gemini_api.devolver(resp)  # corpo lido: reaproveita a conexao
        return True, (resp.status, corpo, nome)
    except Exception as erro:
        estado.liberar(nome, getattr(resp, "_reserva_tokens", 0))
        estado.decrementar_em_voo(nome)
        estado.marcar_erro(nome, f"falha ao ler corpo: {erro}")
        gemini_api.descartar(resp)  # corpo parcial: nao polui o pool
        corpo = json.dumps({"error": {"message": f"falha ao ler resposta de {nome}: {erro}"}}).encode("utf-8")
        return True, (502, corpo, nome)
    finally:
        gemini_api.descartar(resp)  # no-op se ja devolvida (sentinela)


def descartar_stream(resp):
    """Encerra resposta de stream: fecha sem devolver ao pool (corpo parcial
    por definicao). Nunca levanta."""
    try:
        gemini_api.descartar(resp)
    except Exception:
        pass


def abrir_stream(estado, cfg, corpo_bytes):
    corpo_bytes = com_stream_usage(corpo_bytes)
    resp, nome, req_id = _abrir(estado, cfg, corpo_bytes, stream=True)
    return True, (resp, nome, req_id)