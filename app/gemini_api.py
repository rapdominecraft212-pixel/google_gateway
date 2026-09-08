import io
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request


def url_base():
    return (
        os.environ.get("GEMINI_API_BASE") or "https://generativelanguage.googleapis.com/v1beta/openai"
    ).rstrip("/")


def url_base_nativa():
    """/v1beta nativo (generateContent). Deriva do GEMINI_API_BASE quando for mock."""
    explicita = (os.environ.get("GEMINI_API_BASE_NATIVA") or "").strip()
    if explicita:
        return explicita.rstrip("/")
    bruta = (os.environ.get("GEMINI_API_BASE") or "").strip()
    if bruta:
        base = bruta.rstrip("/")
        if base.endswith("/openai"):
            return base[: -len("/openai")]
        return base
    return "https://generativelanguage.googleapis.com/v1beta"


def _requisicao(metodo, caminho, chave, corpo=None, timeout=20):
    headers = {
        "Authorization": f"Bearer {chave}",
        "x-goog-api-key": chave,
        "Content-Type": "application/json",
        "User-Agent": "monitor-gemini/2.0",
    }
    req = urllib.request.Request(url_base() + caminho, method=metodo, data=corpo, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout)


# ---------------------------------------------------------------------------
# Pool de conexoes keep-alive por chave (caminho do chat).
#
# Hoje cada tentativa abre TCP+TLS+DNS do zero (~200-400ms) e sob rajada o
# churn esgota a pilha local (getaddrinfo failed, conexao abortada). O pool
# reusa ate 2 conexoes HTTP/1.1 por chave (= max_conc_por_key), com expiracao
# de ociosas (60s), descarte em qualquer erro de transporte e UMA tentativa
# extra transparente quando a conexao reutilizada morreu do lado servidor
# (caso normal de keep-alive: o servidor pode fechar idle a qualquer hora).
# Retorna http.client.HTTPResponse — o MESMO tipo do urlopen — e converte
# status >= 400 em urllib.error.HTTPError construido, preservando o contrato
# que proxy.py espera (erro.code, erro.read()). Caminhos que nao passam por
# abrir_chat (modelos, validar, TTS) continuam one-shot via _requisicao.
# ---------------------------------------------------------------------------

_POOL_LOCK = threading.Lock()
_POOL = {}  # (base, nome) -> [(conn, ultimo_uso), ...]
_POOL_POR_CHAVE = 2
_POOL_IDLE_SEG = 60.0
_ERROS_CONEXAO_MORTA = (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)
_SEM_HANDLE = object()  # getattr default: distingue "sem pool" de "ja tratado"


# ---------------------------------------------------------------------------
# Taxonomia de falha: "nao alcancamos o Google" != "o Google recusou esta chave".
#
# O erro conceitual que gerava "teto de tentativas (5): getaddrinfo failed" era
# tratar falha de TRANSPORTE (DNS/conexao) como se fosse sinal de SAUDE DA CHAVE:
# o laco andava para a proxima chave e queimava as 5 tentativas, quando nenhuma
# chave tem culpa — a rede e que nao chegou. RedeIndisponivel marca a classe
# "nunca chegamos ao host"; o chamador (proxy._abrir) trata essa classe SEM
# andar de chave e SEM cooldown: re-tenta o pedido com backoff e, se persistir,
# falha honesto ("rede indisponivel"). HTTPError (Google respondeu) continua
# sendo sinal por chave. socket.timeout NAO e daqui: e Google pendurado (por
# chave), tratado como falha comum.
# ---------------------------------------------------------------------------


class RedeIndisponivel(Exception):
    """Falha de transporte: DNS nao resolveu / conexao recusada/morta. Nao e
    culpa da chave; nao deve queimar tentativa nem aplicar cooldown."""

    def __init__(self, causa):
        self.causa = causa
        super().__init__(str(causa))


# ---------------------------------------------------------------------------
# Cache de DNS: TTL + single-flight + stale-while-error + cache negativo.
#
# Sob rajada, N getaddrinfo simultaneos derrubam o resolver do Windows
# ([Errno 11001]). Tres defesas:
#  - single-flight: um lock de resolucao colapsa N buscas frias concorrentes
#    em UMA chamada real (as outras esperam e herdam o resultado);
#  - stale-while-error: se o resolver falha mas HOUVE um IP antes, serve o
#    vencido (frontends do Google mudam raramente; derrubar tudo por DNS e
#    pior que usar IP de minutos atras);
#  - cache negativo: resolver falhou sem stale? marca falha por alguns segundos
#    para nao martelar um resolver morto a cada nova tentativa de chave.
# NAO se apaga o cache inteiro em erro de transporte (isso matava o stale e
# transformava um blip em manada fria); so se marca vencido para revalidar.
# Instalado no import: vale para pool, urlopen, TTS e validacao (mesmo proc).
# ---------------------------------------------------------------------------

_DNS_LOCK = threading.Lock()
_DNS_RESOLVE_LOCK = threading.Lock()  # single-flight: um resolver por vez
_DNS_CACHE = {}  # chave -> (expira, resultado)
_DNS_NEG = {}    # chave -> expira_em (falha recente)
_DNS_TTL_SEG = 60.0
_DNS_NEG_TTL_SEG = 2.0
_DNS_ORIGINAL = socket.getaddrinfo


def _dns_cacheado(host, port, family=0, type=0, proto=0, flags=0):
    chave = (host, port, family, type, proto, flags)
    agora = time.time()
    with _DNS_LOCK:
        achado = _DNS_CACHE.get(chave)
        if achado is not None and achado[0] > agora:
            return achado[1]
        neg = _DNS_NEG.get(chave)
        if neg is not None and neg > agora and achado is None:
            raise socket.gaierror("resolucao em backoff (cache negativo)")
    # frio ou vencido: um resolve, os concorrentes esperam e herdam
    with _DNS_RESOLVE_LOCK:
        agora = time.time()
        with _DNS_LOCK:
            achado = _DNS_CACHE.get(chave)
            if achado is not None and achado[0] > agora:
                return achado[1]  # outro thread preencheu enquanto esperavamos
        try:
            resultado = _DNS_ORIGINAL(host, port, family, type, proto, flags)
        except Exception as erro:
            with _DNS_LOCK:
                velho = _DNS_CACHE.get(chave)
                if velho is not None:
                    return velho[1]  # stale-while-error
                _DNS_NEG[chave] = agora + _DNS_NEG_TTL_SEG
            raise
        with _DNS_LOCK:
            _DNS_CACHE[chave] = (agora + _DNS_TTL_SEG, resultado)
            _DNS_NEG.pop(chave, None)
        return resultado


def _invalidar_dns():
    """Marca entradas como vencidas (forcam re-resolucao) SEM apagar o
    resultado: o stale-while-error ainda serve se o resolver falhar. Apagar
    tudo era o bug que transformava um blip de rede em manada fria."""
    try:
        with _DNS_LOCK:
            for k in list(_DNS_CACHE.keys()):
                _exp, res = _DNS_CACHE[k]
                _DNS_CACHE[k] = (0.0, res)
            _DNS_NEG.clear()
    except Exception:
        pass


def _host_port(base):
    for esquema in ("https://", "http://"):
        if base.startswith(esquema):
            resto = base[len(esquema):]
            break
    else:
        resto = base
    authority = resto.split("/", 1)[0]
    host, _, porta = authority.partition(":")
    try:
        p = int(porta) if porta else (443 if base.startswith("https") else 80)
    except ValueError:
        p = 443
    return host, p


def aquecer():
    """Tira o primeiro pedido do estado frio. Nao basta resolver DNS: um
    getaddrinfo que falha NAO emite pacote, entao nao acorda uma placa Wi-Fi
    suspensa. Abrimos um TCP connect real (SYN) ao host — isso sim religa o
    link. Best-effort, nunca levanta, custo zero de cota."""
    try:
        host, porta = _host_port(url_base())
        try:
            _dns_cacheado(host, porta, 0, socket.SOCK_STREAM)
        except Exception:
            pass
        try:
            s = socket.create_connection((host, porta), timeout=3)
            s.close()
        except Exception:
            pass
    except Exception:
        pass


try:
    socket.getaddrinfo = _dns_cacheado
except Exception:
    pass


def _pool_chave(nome):
    return (url_base(), nome)


def _retirar(nome, timeout):
    """(conn, reutilizada). Nunca levanta: falha -> (None, False)."""
    chave_pool = _pool_chave(nome)
    agora = time.time()
    try:
        with _POOL_LOCK:
            fila = _POOL.get(chave_pool) or []
            while fila:
                conn, usado = fila.pop()
                if agora - usado > _POOL_IDLE_SEG:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    continue
                try:
                    if getattr(conn, "sock", None) is None:
                        try:
                            conn.close()
                        except Exception:
                            pass
                        continue
                    conn.sock.settimeout(timeout)
                    return conn, True
                except Exception:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    continue
            return None, False
    except Exception:
        return None, False


def _guardar(nome, conn):
    try:
        with _POOL_LOCK:
            fila = _POOL.setdefault(_pool_chave(nome), [])
            if len(fila) >= _POOL_POR_CHAVE:
                try:
                    conn.close()
                except Exception:
                    pass
                return
            fila.append((conn, time.time()))
    except Exception:
        try:
            conn.close()
        except Exception:
            pass


def _descartar_conn(conn):
    try:
        conn.close()
    except Exception:
        pass


def _nova_conexao(base, timeout):
    # REGRESSAO: partition(":") deixava o CAMINHO dentro do host
    # ("host.com/v1beta/openai") -> getaddrinfo 11001 eterno com rede perfeita,
    # enquanto o poller (urlopen) funcionava. _host_port separa host e porta.
    host, porta = _host_port(base)
    if base.startswith("https://"):
        import ssl

        import http.client

        return http.client.HTTPSConnection(
            host, porta, timeout=timeout, context=ssl.create_default_context()
        )
    import http.client

    return http.client.HTTPConnection(host, porta, timeout=timeout)


def _caminho_relativo(base, caminho):
    prefixo = None
    for esquema in ("https://", "http://"):
        if base.startswith(esquema):
            resto = base[len(esquema):]
            barra = resto.find("/")
            if barra >= 0:
                prefixo = resto[barra:]
            break
    if not prefixo:
        prefixo = ""
    if caminho.startswith("/"):
        return prefixo + caminho
    return prefixo + "/" + caminho


def _erro_do_corpo(corpo_bytes):
    if not corpo_bytes:
        return None
    try:
        dados = json.loads(corpo_bytes)
    except ValueError:
        return None
    if isinstance(dados, list):
        dados = dados[0] if dados and isinstance(dados[0], dict) else {}
    if not isinstance(dados, dict):
        return None
    erro = dados.get("error")
    return erro if isinstance(erro, dict) else None


def descricao_erro(status, corpo_bytes=None):
    erro = _erro_do_corpo(corpo_bytes)
    codigo = None
    mensagem = None
    if erro:
        codigo = erro.get("code") or erro.get("type") or erro.get("status")
        bruto = erro.get("message")
        if isinstance(bruto, str) and bruto.strip():
            mensagem = " ".join(bruto.split())
    if codigo:
        partes = [f"{codigo} (HTTP {status})"]
    elif erro:
        partes = [f"HTTP {status}"]
    elif corpo_bytes:
        partes = [f"HTTP {status} (corpo ilegivel)"]
    else:
        partes = [f"HTTP {status} (sem corpo)"]
    if mensagem:
        if len(mensagem) > 800:
            mensagem = mensagem[:800] + "..."
        partes.append(mensagem)
    return " | ".join(partes)


def _parse_duracao(texto):
    # Duracao pode chegar como string ("2.4s") ou como objeto proto3
    # ({"seconds": "2", "nanos": 416477402}) — os dois sao aceitos.
    if isinstance(texto, dict):
        try:
            valor = float(texto.get("seconds") or 0) + float(texto.get("nanos") or 0) / 1e9
            return valor if 0 < valor <= 86400 else None
        except (TypeError, ValueError):
            return None
    if not isinstance(texto, str) or not texto:
        return None
    try:
        if texto.endswith("s"):
            valor = float(texto[:-1])
        elif texto.endswith("m"):
            valor = float(texto[:-1]) * 60
        elif texto.endswith("h"):
            valor = float(texto[:-1]) * 3600
        else:
            valor = float(texto)
        if 0 < valor <= 86400:
            return valor
    except ValueError:
        return None
    return None


def retry_delay_seg(corpo_bytes):
    try:
        dados = json.loads(corpo_bytes)
    except ValueError:
        return None
    erro = dados.get("error") if isinstance(dados, dict) else None
    detalhes = erro.get("details") if isinstance(erro, dict) else None
    if isinstance(detalhes, list):
        for item in detalhes:
            if isinstance(item, dict) and item.get("retryDelay") is not None:
                valor = _parse_duracao(item.get("retryDelay"))
                if valor is not None:
                    return valor
                # retryDelay ilegivel NAO aborta: cai para o regex da mensagem.
                # (O early-return daqui devolvia None com "retry in Xs" claro
                # na mensagem e o gateway aplicava cooldown generico.)
    mensagem = erro.get("message") if isinstance(erro, dict) and isinstance(erro.get("message"), str) else None
    if mensagem:
        import re

        achado = re.search(r"retry\s+in\s+([0-9.]+)\s*s", mensagem, re.IGNORECASE)
        if achado:
            try:
                valor = float(achado.group(1))
                if 0 < valor <= 86400:
                    return valor
            except ValueError:
                pass
    return None


def quota_diaria_esgotada(corpo_bytes):
    if not corpo_bytes:
        return False
    try:
        dados = json.loads(corpo_bytes)
    except ValueError:
        return False
    texto = json.dumps(dados, ensure_ascii=False).lower()
    if "per_day" in texto or "per day" in texto:
        return True
    return (retry_delay_seg(corpo_bytes) or 0) >= 3600


def quota_reset_alvo(corpo_bytes):
    """Decide o alvo de reset de um 429 de cota.

    Retorna "mes", "dia" ou None (None = rate-limit transitorio de RPM/TPM;
    a chave volta em segundos via marcar_retry).
    """
    if not corpo_bytes:
        return None
    try:
        dados = json.loads(corpo_bytes)
    except ValueError:
        return None
    texto = json.dumps(dados, ensure_ascii=False).lower()
    if "per_month" in texto or "per month" in texto:
        return "mes"
    if "per_day" in texto or "per day" in texto:
        return "dia"
    if (retry_delay_seg(corpo_bytes) or 0) >= 3600:
        return "dia"
    return None


def quarenta_e_nove_de_capacidade(corpo_bytes):
    """True se o 429 e throttle de CAPACIDADE do modelo, nao quota da chave.

    Medido em 2026-09-07 (teste_cota_real: 76 pedidos reais, zero 429 de
    quota a 8 req/min/chave; e a tempestade das 16:23 misturou 503 e 429
    generico no mesmo episodio). O 429 de quota de verdade traz a metrica
    ('Quota exceeded for metric: ...'); o de capacidade nao: 'Resource has
    been exhausted (e.g. check quota)' seco. Sem corpo tambem conta como
    capacidade: sem a metrica nao ha como culpar a chave.
    """
    if not corpo_bytes:
        return True
    try:
        dados = json.loads(corpo_bytes)
    except ValueError:
        return True
    texto = json.dumps(dados, ensure_ascii=False).lower()
    return "quota exceeded for metric" not in texto


def listar_modelos(chave, timeout=20):
    try:
        with _requisicao("GET", "/models", chave, timeout=timeout) as resp:
            corpo = resp.read()
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
        return None
    try:
        dados = json.loads(corpo)
    except ValueError:
        return None
    ids = []
    for item in dados.get("data") or []:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            ids.append(item["id"].removeprefix("models/"))
    return ids


def validar_chave(chave, timeout=20):
    try:
        with _requisicao("GET", "/models", chave, timeout=timeout) as resp:
            resp.read()
    except urllib.error.HTTPError as erro:
        return False, f"chave rejeitada: {descricao_erro(erro.code, erro.read())}"
    except urllib.error.URLError as erro:
        return False, f"falha de rede: {erro.reason}"
    except OSError as erro:
        return False, f"erro de conexao: {erro}"
    return True, {"modelos": "ok"}


def _relaxar_timeout_corpo(resp, timeout=120):
    """Apos os headers (TTFB), o corte agressivo de episodio nao vale mais:
    stalls de corpo/stream sao normais (geracao longa). Restaura o teto
    padrao no socket para nao matar stream legitimo no meio."""
    try:
        fp = getattr(resp, "fp", None)
        raw = getattr(fp, "raw", None)
        sock = getattr(raw, "_sock", None) or raw
        if sock is not None and hasattr(sock, "settimeout"):
            sock.settimeout(timeout)
    except Exception:
        pass


def abrir_chat(chave, corpo_bytes, timeout=120, nome=None):
    """POST /chat/completions com keep-alive (pool por chave).

    `nome` e so a etiqueta do pool (default: o proprio segredo; nunca logado).
    Comportamento de erro identico ao urlopen: status >= 400 vira
    urllib.error.HTTPError (construido, com .code/.read()); rede vira
    URLError/OSError. Conexao reutilizada que morreu do lado servidor
    (caso normal de keep-alive) ganha UMA tentativa extra transparente em
    conexao fresca — timeouts nunca sao re-tentados (o original pode ainda
    estar processando no Google; re-tentar gastaria cota 2x).
    """
    etiqueta = nome if nome else chave
    base = url_base()
    headers = {
        "Authorization": f"Bearer {chave}",
        "x-goog-api-key": chave,
        "Content-Type": "application/json",
        "User-Agent": "monitor-gemini/2.0",
        "Connection": "keep-alive",
    }
    caminho = _caminho_relativo(base, "/chat/completions")
    import http.client

    host, _porta = _host_port(base)
    for tentativa in range(2):
        conn, reutilizada = _retirar(etiqueta, timeout)
        if conn is None:
            try:
                conn = _nova_conexao(base, timeout)
            except Exception as erro:
                raise RedeIndisponivel(erro)
            # Forca DNS+TCP+TLS AGORA: qualquer falha aqui = nunca alcancamos
            # o host -> RedeIndisponivel (nao e culpa da chave).
            try:
                conn.connect()
            except socket.timeout as erro:
                _descartar_conn(conn)
                raise RedeIndisponivel(erro)
            except (socket.gaierror, ConnectionRefusedError, OSError) as erro:
                _descartar_conn(conn)
                _invalidar_dns()
                raise RedeIndisponivel(erro)
            reutilizada = False
        try:
            conn.request("POST", caminho, body=corpo_bytes, headers=headers)
            resp = conn.getresponse()
        except _ERROS_CONEXAO_MORTA + (http.client.RemoteDisconnected,) as erro:
            _descartar_conn(conn)
            _invalidar_dns()  # IP pode ter mudado; mantem stale p/ socorro
            if reutilizada and tentativa == 0:
                continue  # servidor fechou idle: refaz em conexao fresca
            raise RedeIndisponivel(erro)  # conexao morreu em uso = transporte
        except socket.timeout:
            # conectamos e enviamos; Google demorando = hang POR CHAVE (nao
            # rede): deixa o proxy tratar como falha comum e andar a chave.
            _descartar_conn(conn)
            raise
        except http.client.HTTPException as erro:
            _descartar_conn(conn)
            raise RedeIndisponivel(erro)
        if resp.status >= 400:
            try:
                corpo_erro = resp.read()
            except Exception:
                corpo_erro = b""
            _guardar(etiqueta, conn)  # erro com corpo lido: socket limpo
            raise urllib.error.HTTPError(
                base + caminho, resp.status, resp.reason, resp.headers,
                io.BytesIO(corpo_erro),
            )
        try:
            resp._pool = (etiqueta, conn)
        except Exception:
            pass
        _relaxar_timeout_corpo(resp, 120)
        return resp
    raise RedeIndisponivel("falha de conexao keep-alive apos 1 re-tentativa")


def devolver(resp):
    """Devolve a conexao ao pool SE a resposta foi totalmente lida
    (resp.length == 0); senao fecha. Marca como tratada (None) para que um
    descartar() posterior nao feche o socket do pool. Nunca levanta. Sem
    handle (_SEM_HANDLE) = resposta de outro caminho: fecha por compat.
    Mocks de teste (sem length numerico) caem no fechar, sem efeito colateral.
    """
    try:
        handle = getattr(resp, "_pool", _SEM_HANDLE)
    except Exception:
        return
    if handle is None:
        return
    if handle is _SEM_HANDLE:
        try:
            resp.close()
        except Exception:
            pass
        return
    try:
        resp._pool = None
    except Exception:
        pass
    try:
        etiqueta, conn = handle
    except Exception:
        try:
            resp.close()
        except Exception:
            pass
        return
    try:
        if getattr(resp, "length", None) == 0:
            _guardar(etiqueta, conn)
            return
    except Exception:
        pass
    _descartar_conn(conn)


def descartar(resp):
    """Fecha a conexao sem devolver ao pool (streams, corpos parciais).
    Se ja foi devolvida (handle None), nao faz nada — o socket pertence ao
    pool. Nunca levanta."""
    try:
        handle = getattr(resp, "_pool", _SEM_HANDLE)
    except Exception:
        return
    if handle is None:
        return
    if handle is _SEM_HANDLE:
        try:
            resp.close()
        except Exception:
            pass
        return
    try:
        resp._pool = None
    except Exception:
        pass
    try:
        _, conn = handle
    except Exception:
        try:
            resp.close()
        except Exception:
            pass
        return
    _descartar_conn(conn)


def gerar_fala(chave, modelo, texto, voz="Kore", timeout=120):
    """TTS nativo: POST /v1beta/models/{model}:generateContent.

    Retorna (mime, audio_b64). Levanta HTTPError/URLError como abrir_chat.
    """
    corpo = json.dumps(
        {
            "contents": [{"parts": [{"text": texto}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voz}}
                },
            },
        }
    ).encode("utf-8")
    caminho = f"/models/{modelo}:generateContent"
    headers = {
        "x-goog-api-key": chave,
        "Content-Type": "application/json",
        "User-Agent": "monitor-gemini/2.0",
    }
    req = urllib.request.Request(
        url_base_nativa() + caminho, method="POST", data=corpo, headers=headers
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        dados = json.loads(resp.read() or b"{}")
    for cand in (dados.get("candidates") or []):
        partes = ((cand.get("content") or {}).get("parts")) or []
        for parte in partes:
            inline = (parte or {}).get("inlineData") or (parte or {}).get("inline_data")
            if isinstance(inline, dict) and inline.get("data"):
                return inline.get("mimeType") or inline.get("mime_type") or "audio/L16;rate=24000", inline["data"]
    raise ValueError("resposta TTS sem audio (mime/data ausentes)")


def extrair_audio_b64(dados):
    """Extrai (mime, b64) de um JSON generateContent ja decodificado (uso em testes)."""
    for cand in (dados.get("candidates") or []):
        partes = ((cand.get("content") or {}).get("parts")) or []
        for parte in partes:
            inline = (parte or {}).get("inlineData") or (parte or {}).get("inline_data")
            if isinstance(inline, dict) and inline.get("data"):
                return inline.get("mimeType") or inline.get("mime_type") or "audio/L16;rate=24000", inline["data"]
    return None, None
