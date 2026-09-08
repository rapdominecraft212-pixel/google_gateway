#!/usr/bin/env python3
"""Simula uma tarefa de agente multi-etapas (ex.: "crie um app de jornalismo")
contra o gateway real e loga o comportamento: latencia por etapa, crescimento
do contexto, e quantos 429 o gateway varreu em cada requisicao.

Uso:
    python teste\\simular_tarefa.py [modelo] [etapas] [bytes_por_etapa]
    padrao: gemini-3.6-flash 6 3000

Saida:
    logs/tarefa_simulada.log   (log detalhado)
    stdout                     (resumo)
"""
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
GATEWAY = "http://127.0.0.1:8011/v1/chat/completions"
DB = BASE / "historico.db"
LOGDIR = BASE / "logs"
LOGDIR.mkdir(exist_ok=True)
LOGFILE = LOGDIR / "tarefa_simulada.log"

MODELO = sys.argv[1] if len(sys.argv) > 1 else "gemini-3.6-flash"
ETAPAS = int(sys.argv[2]) if len(sys.argv) > 2 else 6
BYTES_ETAPA = int(sys.argv[3]) if len(sys.argv) > 3 else 3000

_log_fh = open(LOGFILE, "w", encoding="utf-8")


def log(msg):
    linha = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    _log_fh.write(linha + "\n")
    _log_fh.flush()


def db_max_id():
    conn = sqlite3.connect(DB)
    try:
        return conn.execute("SELECT COALESCE(MAX(id),0) FROM requisicoes").fetchone()[0]
    finally:
        conn.close()


def aprox_tokens(texto):
    return len(texto) // 4


def enviar(mensagens):
    corpo = {"model": MODELO, "messages": mensagens, "stream": False}
    dados = json.dumps(corpo).encode("utf-8")
    req = urllib.request.Request(
        GATEWAY,
        data=dados,
        headers={"Content-Type": "application/json", "Authorization": "Bearer gateway-local"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            corpo_resp = resp.read()
            ms = int((time.time() - t0) * 1000)
            obj = json.loads(corpo_resp)
            uso = obj.get("usage") or {}
            conteudo = ((obj.get("choices") or [{}])[0].get("message") or {}).get("content", "")
            return {
                "ok": True,
                "status": resp.status,
                "ms": ms,
                "tokens_in": uso.get("prompt_tokens", 0),
                "tokens_out": uso.get("completion_tokens", 0),
                "conteudo": conteudo or "",
            }
    except urllib.error.HTTPError as erro:
        ms = int((time.time() - t0) * 1000)
        try:
            texto = erro.read().decode("utf-8", "ignore")[:400]
        except Exception:
            texto = ""
        return {"ok": False, "status": erro.code, "ms": ms, "erro": texto}
    except Exception as erro:
        ms = int((time.time() - t0) * 1000)
        return {"ok": False, "status": None, "ms": ms, "erro": str(erro)[:400]}


def main():
    log(f"=== INICIO modelo={MODELO} etapas={ETAPAS} bytes_por_etapa={BYTES_ETAPA} ===")
    start_id = db_max_id()
    log(f"db start_id={start_id}")

    mensagens = [
        {"role": "system", "content": "Voce e um engenheiro de software sênior. Construa um app de jornalismo completo."},
        {"role": "user", "content": "Crie um app de jornalismo com frontend, backend e banco de dados. Va passo a passo criando os arquivos."},
    ]

    resultados = []
    for etapa in range(1, ETAPAS + 1):
        ctx_chars = sum(len(m.get("content", "")) for m in mensagens)
        r = enviar(mensagens)
        resultados.append((etapa, ctx_chars, r))
        if r["ok"]:
            log(
                f"etapa {etapa:02d} OK  ctx~{aprox_tokens('x'*ctx_chars)}tok "
                f"ms={r['ms']} tin={r['tokens_in']} tout={r['tokens_out']}"
            )
            mensagens.append({"role": "assistant", "content": r["conteudo"][:2000]})
        else:
            log(f"etapa {etapa:02d} FALHOU status={r['status']} ms={r['ms']} erro={r.get('erro','')[:200]}")
            mensagens.append({"role": "assistant", "content": "(etapa falhou)"})
        enchimento = (
            f"Resultado da ferramenta (etapa {etapa}): arquivo criado com sucesso.\n"
            + ("conteudo do arquivo gerado. " * (BYTES_ETAPA // 24))
        )
        mensagens.append({"role": "user", "content": enchimento})

    time.sleep(2)
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT id, chave, status, ok, ms, tokens_in, tokens_out, erro, quando "
        "FROM requisicoes WHERE id > ? ORDER BY id",
        (start_id,),
    ).fetchall()
    conn.close()

    n200 = sum(1 for r in rows if r[2] == 200)
    n429 = sum(1 for r in rows if r[2] == 429)
    n503 = sum(1 for r in rows if r[2] == 503)
    outros = len(rows) - n200 - n429 - n503
    log(f"--- DB durante o teste: total={len(rows)} 200={n200} 429={n429} 503={n503} outros={outros}")

    por_chave = {}
    for r in rows:
        chave = r[1]
        d = por_chave.setdefault(chave, {"200": 0, "429": 0, "503": 0})
        if r[2] == 200:
            d["200"] += 1
        elif r[2] == 429:
            d["429"] += 1
        elif r[2] == 503:
            d["503"] += 1
    for chave, d in sorted(por_chave.items()):
        log(f"  chave {chave}: 200={d['200']} 429={d['429']} 503={d['503']}")

    log("--- linhas detalhadas do DB (id chave status ms tin tout)")
    for r in rows:
        log(f"  id={r[0]} chave={r[1]} status={r[2]} ms={r[4]} tin={r[5]} tout={r[6]}")

    oks = [x for x in resultados if x[2]["ok"]]
    if oks:
        medias = [x[2]["ms"] for x in oks]
        log(f"resumo: etapas_ok={len(oks)}/{ETAPAS} latencia_media={sum(medias)//len(medias)}ms "
            f"min={min(medias)}ms max={max(medias)}ms")
    log("=== FIM ===")
    _log_fh.close()

    print(f"log completo: {LOGFILE}")
    print(f"etapas OK: {len(oks)}/{ETAPAS}")
    if oks:
        print(f"latencia por etapa (ms): {[x[2]['ms'] for x in resultados]}")
    print(f"requests no gateway durante o teste: {len(rows)} (200={n200}, 429={n429}, 503={n503})")


if __name__ == "__main__":
    main()
