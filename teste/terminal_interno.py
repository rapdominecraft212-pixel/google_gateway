#!/usr/bin/env python3
"""Terminal interno: ve a maquinaria do gateway trabalhando, AO VIVO e legivel.

O gateway grava cada pedido do cliente em logs/eventos-diagnostico.log
(eventos pedido_*): inicio -> rodadas (quais chaves, por que) -> cada falha
(o que o Google disse, cooldown aplicado, quanto do balde a chave tinha
usado NA HORA) -> esperas -> desfecho. Este terminal segue o arquivo em
tempo real e imprime cada evento em uma linha que um humano entende.

Uso:
    python teste\\terminal_interno.py                      (segue ao vivo, Ctrl+C sai)
    python teste\\terminal_interno.py --duracao 30         (segue 30s e sai)
    python teste\\terminal_interno.py --msg "diga apenas ok"  (mensagem REAL + maquinaria ao vivo)
    python teste\\terminal_interno.py --desde 18:15 --ate 18:25   (replay do periodo)
    python teste\\terminal_interno.py --ultimos 50          (replay dos ultimos 50 eventos)
    python teste\\terminal_interno.py --req a1b2c3           (replay de um pedido)
    python teste\\terminal_interno.py --tudo                (inclui heartbeats)

Leitura local do log: zero cota, zero rede. So --msg gasta tokens (~1-2,
max_tokens=64) e o custo real e reportado no fim.
"""
import argparse
import collections
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
LOG = RAIZ / "logs" / "eventos-diagnostico.log"
sys.path.insert(0, str(RAIZ))


def dur(ms):
    try:
        return f"{int(ms) / 1000:.2f}s"
    except (TypeError, ValueError):
        return "?"


def resumo_detalhe(d, limite=140):
    partes = []
    for k, v in d.items():
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False)
        partes.append(f"{k}={v}")
    txt = " ".join(partes)
    return txt[:limite]


def linha_evento(o, tudo=False):
    ev = str(o.get("evento") or "")
    fonte = str(o.get("fonte") or "")
    d = o.get("detalhe") or {}
    ts = str(o.get("ts") or "")[11:23]
    if ev == "heartbeat":
        if not tudo:
            return None
        rac = d.get("racers") or {}
        return (f"{ts}  [batida] ativas={d.get('chaves_ativas')} "
                f"racers_alvo={rac.get('alvo_ma')} em_voo={rac.get('pedidos_em_voo')} "
                f"uptime={d.get('uptime_s')}s")
    if ev == "pedido_inicio":
        return (f"{ts} INICIO req={d.get('req')} modelo={d.get('modelo')} "
                f"est_tokens={d.get('tokens_est')} racers={d.get('racers_base')} "
                f"alvo_ma={d.get('alvo_ma')}")
    if ev == "pedido_rodada":
        chaves = d.get("chaves") or []
        return (f"{ts}   rodada {d.get('rodada')}: {len(chaves)} chave(s) {chaves}"
                f"  -> {str(d.get('razao') or '')[:120]}")
    if ev == "pedido_falha":
        extra = ""
        if d.get("quota_real"):
            extra = f"  QUOTA-REAL retry_google={d.get('retry_google_s')}s"
        return (f"{ts}   FALHA {d.get('chave')} [{d.get('tipo')}] st={d.get('status')} "
                f"cd={d.get('cooldown_s')}s balde={d.get('reqs_60s')}req/"
                f"{d.get('tokens_60s')}tok{extra}")
    if ev == "pedido_espera":
        return (f"{ts}   ESPERA {d.get('segundos')}s motivo={d.get('motivo')} "
                f"ciclo={d.get('ciclo')}")
    if ev == "pedido_fim":
        extra = ""
        if d.get("detalhe"):
            extra = f"  :: {str(d.get('detalhe'))[:140]}"
        return (f"{ts} FIM req={d.get('req')} saida={d.get('saida')} "
                f"cliente={d.get('status_cliente')} total={dur(d.get('ms_total'))} "
                f"queimadas={d.get('chaves_queimadas')}{extra}")
    return f"{ts}  [{fonte}] {ev} {resumo_detalhe(d)}"


def imprimir(o, tudo=False):
    linha = linha_evento(o, tudo)
    if linha is not None:
        print(linha, flush=True)


def ler_log():
    try:
        texto = LOG.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    evs = []
    for ln in texto.splitlines():
        try:
            evs.append(json.loads(ln))
        except ValueError:
            continue
    return evs


def no_periodo(o, desde, ate):
    hhmm = str(o.get("ts") or "")[11:16]
    if desde and hhmm < desde:
        return False
    if ate and hhmm > ate:
        return False
    return True


def replay(args):
    evs = ler_log()
    if args.req:
        grupo = [o for o in evs if (o.get("detalhe") or {}).get("req") == args.req]
        if not grupo:
            print(f"req {args.req} nao encontrado")
            return
        for o in grupo:
            imprimir(o, tudo=True)
        return
    if args.desde or args.ate:
        evs = [o for o in evs if no_periodo(o, args.desde, args.ate)]
    if args.ultimos:
        evs = evs[-args.ultimos:]
    if not evs:
        print("sem eventos no periodo/pedido pedido.")
        return
    for o in evs:
        imprimir(o, tudo=True)


def seguir(duracao, desde, tudo, eventos_capturados=None, cabecalho=True):
    """Fim de arquivo -> imprime cada linha nova conforme entra. Retorna quando
    duracao esgota (se definida) ou em Ctrl+C."""
    pos = 0
    if LOG.exists():
        with open(LOG, "r", encoding="utf-8", errors="ignore") as fh:
            fh.seek(0, 2)
            pos = fh.tell()
    inicio = time.time()
    if cabecalho:
        if duracao is None:
            print(f"seguindo {LOG.name} ao vivo (Ctrl+C para sair)...", flush=True)
        else:
            print(f"seguindo {LOG.name} por {duracao}s...", flush=True)
    try:
        while True:
            if duracao is not None and time.time() - inicio >= duracao:
                return True
            try:
                tamanho = LOG.stat().st_size
            except FileNotFoundError:
                time.sleep(0.3)
                continue
            if tamanho < pos:
                pos = 0
            if tamanho > pos:
                with open(LOG, "r", encoding="utf-8", errors="ignore") as fh:
                    fh.seek(pos)
                    for ln in fh:
                        try:
                            o = json.loads(ln)
                        except ValueError:
                            continue
                        if eventos_capturados is not None:
                            eventos_capturados.append(o)
                        imprimir(o, tudo=tudo)
                    pos = fh.tell()
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\nparado.")
        return False


def escolher_modelo(cfg):
    modelos = cfg.get("modelos") or []
    for m in modelos:
        if "3.7" not in str(m):
            return str(m)
    return "gemini-3.5-flash-lite"


def enviar_mensagem(texto, cfg, host, porta):
    modelo = escolher_modelo(cfg)
    corpo = {
        "model": modelo,
        "messages": [{"role": "user", "content": texto}],
        "max_tokens": 64,
    }
    headers = {"Content-Type": "application/json"}
    token = (cfg.get("gateway_token") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"http://{host}:{porta}/v1/chat/completions",
        data=json.dumps(corpo).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            dados = json.loads(resp.read())
            status = resp.status
    except urllib.error.HTTPError as erro:
        corpo_erro = erro.read()
        try:
            dados = json.loads(corpo_erro)
        except ValueError:
            dados = {"raw": corpo_erro.decode("utf-8", "ignore")}
        status = erro.code
    except Exception as erro:
        return modelo, None, int((time.time() - t0) * 1000), f"falha de rede: {erro}", 0, 0
    ms = int((time.time() - t0) * 1000)
    if status != 200:
        return modelo, dados, ms, f"HTTP {status}", 0, 0
    try:
        texto_resp = (dados.get("choices") or [{}])[0].get("message", {}).get("content", "")
    except (AttributeError, IndexError, TypeError):
        texto_resp = ""
    uso = dados.get("usage") or {}
    tin = int(uso.get("prompt_tokens") or 0)
    tout = int(uso.get("completion_tokens") or 0)
    return modelo, {"status": status, "texto": texto_resp}, ms, None, tin, tout


def resumo_msg(evs, t_envio, modelo, resposta, ms, erro, tin, tout):
    t_marca = time.strftime("%H:%M:%S", time.localtime(t_envio))
    print("\n" + "=" * 62)
    print(f"RESUMO  modelo={modelo}  resposta={dur(ms)}")
    if erro:
        print(f"  resultado: {erro}")
        if isinstance(resposta, dict):
            print(f"  corpo: {json.dumps(resposta, ensure_ascii=False)[:300]}")
    else:
        txt = resposta.get("texto") or ""
        print(f"  resposta: {txt[:300]}")
    reqs = []
    for o in evs:
        if o.get("evento") == "pedido_inicio" and str(o.get("ts") or "")[11:19] >= t_marca:
            reqs.append((o.get("detalhe") or {}).get("req"))
    if reqs:
        print(f"  pedidos vistos na janela: {len(set(reqs))}")
        for o in evs:
            if (o.get("detalhe") or {}).get("req") in reqs:
                linha = linha_evento(o, tudo=True)
                if linha:
                    print(f"    {linha}")
    print(f"  custo real: {tin} tokens entrada + {tout} tokens saida")
    print("=" * 62)


def principal():
    ap = argparse.ArgumentParser(description="Terminal interno do gateway (trilha ao vivo)")
    ap.add_argument("--msg", default=None, help="envia uma mensagem REAL ao gateway e assiste a maquinaria")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--porta", type=int, default=None, help="default: config.json")
    ap.add_argument("--duracao", type=int, default=None, help="segue por N segundos e sai")
    ap.add_argument("--desde", default=None, help="replay: HH:MM")
    ap.add_argument("--ate", default=None, help="replay: HH:MM")
    ap.add_argument("--ultimos", type=int, default=None, help="replay: ultimos N eventos")
    ap.add_argument("--req", default=None, help="replay: um req_id especifico")
    ap.add_argument("--tudo", action="store_true", help="inclui heartbeats")
    args = ap.parse_args()

    if args.req or args.desde or args.ate or args.ultimos:
        replay(args)
        return

    if args.msg:
        from app import config as config_mod
        cfg = config_mod.carregar()
        porta = args.porta or int(cfg.get("porta", 8011))
        captura = []
        print(f"enviando mensagem real ({escolher_modelo(cfg)}) e abrindo a caixa-preta...")
        t_envio = time.time()
        resultado = {}

        import threading

        def rodar():
            resultado["vals"] = enviar_mensagem(args.msg, cfg, args.host, porta)

        th = threading.Thread(target=rodar, daemon=True)
        # Marca a posicao do log ANTES de disparar o pedido: se o gateway
        # gravar o INICIO enquanto a thread de exibio ainda faz seek, a
        # primeira linha do proprio pedido nao escapa.
        pos_inicial = LOG.stat().st_size if LOG.exists() else 0
        th.start()
        # Visualizacao ao vivo em thread propria; encerra quando surge o
        # pedido_fim na trilha (sucesso OU falha), nunca por tempo fixo:
        # se o pedido demora 80s na tempestade, a visao acompanha inteira.
        parar_exib = threading.Event()

        def exibir():
            pos = pos_inicial
            while True:
                try:
                    tamanho = LOG.stat().st_size
                except FileNotFoundError:
                    tamanho = 0
                if tamanho < pos:
                    pos = 0
                if tamanho > pos:
                    with open(LOG, "r", encoding="utf-8", errors="ignore") as fh:
                        fh.seek(pos)
                        for ln in fh:
                            try:
                                o = json.loads(ln)
                            except ValueError:
                                continue
                            captura.append(o)
                            imprimir(o, tudo=True)
                        pos = fh.tell()
                # drena o que chegou entre o ultimo poll e o sinal de parar:
                # sem isso o FIM do proprio pedido podia ficar de fora da
                # visao ao vivo (so aparecia no replay)
                if parar_exib.is_set():
                    return
                time.sleep(0.2)

        th_ex = threading.Thread(target=exibir, daemon=True)
        th_ex.start()

        vistos_fim = set()

        def aguardar_fim():
            time.sleep(0.5)
            while th.is_alive():
                for o in list(captura):
                    if o.get("evento") == "pedido_fim":
                        r = (o.get("detalhe") or {}).get("req")
                        if r and r not in vistos_fim:
                            vistos_fim.add(r)
                            return
                time.sleep(0.3)

        th_fim = threading.Thread(target=aguardar_fim, daemon=True)
        th_fim.start()
        th_fim.join(300)
        parar_exib.set()
        time.sleep(0.3)
        th.join(5)
        modelo, resposta, ms, erro, tin, tout = resultado.get("vals", (None, None, 0, "sem resposta", 0, 0))
        resumo_msg(captura, t_envio, modelo, resposta, ms, erro, tin, tout)
        return

    seguir(args.duracao, args.desde, args.tudo)


if __name__ == "__main__":
    principal()
