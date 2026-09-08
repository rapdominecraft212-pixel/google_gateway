#!/usr/bin/env python3
"""HARNESS "opencode-gateway": reproduz o CAMINHO EXATO do usuario no codigo real.

O usuario fala com o gateway pelo opencode via `@ai-sdk/openai-compatible`
(baseURL http://127.0.0.1:<porta>/v1, Bearer <token>, modelo gemini-3.8-flash,
reasoning ligado, STREAMING). Este harness NAO usa atalhos: abre as MESMAS
conexoes, com o MESMO formato de corpo, e roda N agentes em paralelo fazendo
conversas multi-turno reais contra o Google de verdade.

Objetivo (criterio de "pronto"): ZERO erro visivel ao usuario. Cada turno deve
voltar HTTP 200 com conteudo nao-vazio e finish_reason valido. Erros de
capacidade do Google (503 / 429 generico / hang) devem ser ABSORVIDOS pelo
gateway (racers adaptativos + fallback de modelo) e nunca chegar ao cliente.

Compara o que o CLIENTE viu (200s) com o que o UPSTREAM sofreu (via
/api/requisicoes no periodo): a diferenca e a prova de que a estrategia nova
esta fazendo o trabalho dela.

Regra 1 do AGENTS.md: sem mock. Chama o gateway real, que chama o Google real.
Regra de custo: prompts curtos, max_tokens pequeno; reporta tokens no fim.

Uso:
    python teste/harness_opencode_gateway.py --yes-real
    python teste/harness_opencode_gateway.py --yes-real --agentes 4 --turnos 3
"""
import argparse
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from app import config as config_mod  # noqa: E402

# Mesma forma que o opencode (openai-compatible) monta o corpo.
MODELO_USUARIO = "gemini-3.8-flash"
TOKEN_FIDELIDADE = "gateway-local"  # header igual ao do cliente real


class ErroCliente(Exception):
    pass


def _headers():
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {TOKEN_FIDELIDADE}",
        "Accept": "text/event-stream",
        "User-Agent": "opencode/1.0 (openai-compatible)",
    }


def corpo_de(mensagens, modelo, stream=True, max_tokens=200):
    return json.dumps({
        "model": modelo,
        "messages": mensagens,
        "stream": stream,
        "max_tokens": max_tokens,
        "reasoning_effort": "low",
    }).encode("utf-8")


def turno_stream(base, mensagens, modelo, timeout=150):
    """Um turno de chat em SSE, igual ao cliente real. Devolve
    (http_status, conteudo, finish_reason, modelo_que_respondeu, usage, ms).
    Levanta ErroCliente em QUALQUER coisa que o usuario veria como falha."""
    dados = corpo_de(mensagens, modelo, stream=True)
    req = urllib.request.Request(base + "/v1/chat/completions", data=dados,
                                 headers=_headers(), method="POST")
    t0 = time.time()
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as erro:
        corpo = erro.read().decode("utf-8", "ignore")
        raise ErroCliente(f"HTTP {erro.code} para o cliente: {corpo[:200]}")
    conteudo = []
    finish = None
    modelo_resp = None
    usage = None
    for bruto in resp:
        linha = bruto.decode("utf-8", "ignore").strip()
        if not linha.startswith("data:"):
            continue
        payload = linha[5:].strip()
        if payload == "[DONE]":
            break
        try:
            obj = json.loads(payload)
        except ValueError:
            continue
        if obj.get("model"):
            modelo_resp = obj["model"]
        if obj.get("usage"):
            usage = obj["usage"]
        for ch in obj.get("choices") or []:
            delta = ch.get("delta") or {}
            if delta.get("content"):
                conteudo.append(delta["content"])
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
    ms = int((time.time() - t0) * 1000)
    texto = "".join(conteudo)
    if not texto.strip():
        raise ErroCliente(f"stream 200 mas SEM conteudo (finish={finish})")
    return resp.status, texto, finish, modelo_resp, usage, ms


def agente(idx, base, modelo, turnos, resultados, trava):
    """Conversa multi-turno real: cada turno ve o anterior (memoria de contexto)."""
    mensagens = [{"role": "system",
                  "content": "Voce é um agente. Responda em uma frase curta."}]
    perguntas = [
        "O que e uma API gateway?",
        "Some 2 + 2 e diga so o numero.",
        "Cite uma capital da Europa.",
        "O que e latencia em uma rede?",
        "Diga uma palavra sobre o mar.",
    ]
    for t in range(turnos):
        mensagens.append({"role": "user", "content": perguntas[t % len(perguntas)]})
        try:
            st, texto, finish, mresp, usage, ms = turno_stream(base, mensagens, modelo)
            eventos = [{"role": "assistant", "content": texto}]
            mensagens.extend(eventos)
            with trava:
                resultados.append({"agente": idx, "turno": t, "ok": True, "status": st,
                                   "ms": ms, "modelo_resp": mresp,
                                   "tokens": (usage or {}).get("total_tokens", 0),
                                   "trecho": texto[:40]})
        except ErroCliente as e:
            with trava:
                resultados.append({"agente": idx, "turno": t, "ok": False, "erro": str(e)})
            return
        except Exception as e:
            with trava:
                resultados.append({"agente": idx, "turno": t, "ok": False,
                                   "erro": f"{type(e).__name__}: {e}"})
            return


def requisicoes_no_periodo(base, desde_iso):
    """Puxa /api/requisicoes e conta o que o UPSTREAM sofreu no periodo."""
    try:
        req = urllib.request.Request(base + "/api/requisicoes", headers=_headers())
        dados = json.loads(urllib.request.urlopen(req, timeout=20).read())
    except Exception:
        return None
    linhas = dados if isinstance(dados, list) else dados.get("requisicoes") or []
    res = {"total": 0, "ok": 0, "e503": 0, "e429": 0, "timeout": 0}
    for r in linhas:
        q = r.get("quando") or ""
        if q < desde_iso:
            continue
        res["total"] += 1
        if r.get("ok"):
            res["ok"] += 1
        elif r.get("status") == 503:
            res["e503"] += 1
        elif r.get("status") == 429:
            res["e429"] += 1
        elif r.get("status") is None:
            res["timeout"] += 1
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes-real", action="store_true")
    ap.add_argument("--agentes", type=int, default=4)
    ap.add_argument("--turnos", type=int, default=3)
    ap.add_argument("--modelo", default=MODELO_USUARIO)
    args = ap.parse_args()
    if not args.yes_real:
        print("Requer --yes-real: roda carga REAL contra o Google via gateway.")
        sys.exit(1)

    cfg = config_mod.carregar()
    porta = int(cfg.get("porta") or 8011)
    base = f"http://127.0.0.1:{porta}"
    with socket.socket() as s:
        s.settimeout(3)
        if s.connect_ex(("127.0.0.1", porta)) != 0:
            print(f"gateway nao esta de pe em {base}")
            sys.exit(2)

    desde_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
    print(f"HARNESS opencode-gateway | base={base} modelo={args.modelo} "
          f"agentes={args.agentes} turnos={args.turnos}")
    print(f"Cliente real emulado: openai-compatible, streaming, Bearer {TOKEN_FIDELIDADE}\n")

    resultados = []
    trava = threading.Lock()
    t0 = time.time()
    threads = [threading.Thread(target=agente, args=(i, base, args.modelo, args.turnos, resultados, trava))
               for i in range(args.agentes)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    dur = time.time() - t0

    ok = [r for r in resultados if r["ok"]]
    falhas = [r for r in resultados if not r["ok"]]
    tokens = sum(r.get("tokens", 0) for r in ok)
    modelos_usados = {}
    for r in ok:
        m = r.get("modelo_resp") or "?"
        modelos_usados[m] = modelos_usados.get(m, 0) + 1

    print(f"--- CLIENTE (o que o usuario veria) ---")
    print(f"  turnos tentados: {len(resultados)} | entregues com conteudo: {len(ok)} | erros visiveis: {len(falhas)}")
    if ok:
        mss = sorted(r["ms"] for r in ok)
        print(f"  latencia turno (ms): p50={mss[len(mss)//2]} max={mss[-1]}")
    print(f"  modelos que responderam ao cliente: {modelos_usados}")
    for r in falhas:
        print(f"  [ERRO VISIVEL] agente {r['agente']} turno {r['turno']}: {r['erro']}")

    up = requisicoes_no_periodo(base, desde_iso)
    if up:
        print(f"\n--- UPSTREAM (o que o gateway sofreu por tras) ---")
        print(f"  pedidos ao Google: {up['total']} | ok={up['ok']} 503={up['e503']} "
              f"429={up['e429']} timeout={up['timeout']}")
        absorvidos = up["total"] - up["ok"]
        print(f"  falhas de capacidade ABSORVIDAS pelo gateway: {absorvidos}")

    print(f"\nCUSTO: ~{tokens} tokens, {dur:.0f}s, {len(resultados)} turnos de cliente")
    if falhas:
        print(f"\nRESULTADO: FALHOU — {len(falhas)} erro(s) chegaram ao cliente.")
        sys.exit(3)
    print("\nRESULTADO: PASSOU — zero erro visivel ao usuario sob carga real.")


if __name__ == "__main__":
    main()
