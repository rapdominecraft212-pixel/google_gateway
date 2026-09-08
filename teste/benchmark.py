"""Harness de benchmark para os dois objetivos do gateway.

Objetivo 1 (throughput): o usuario consegue enviar e receber ate N mensagens
completas dentro de 1 minuto, com o contexto real (~188k tokens/pedido).

Objetivo 2 (zero erro visivel): o usuario final NUNCA ve mensagem de erro.
Ou o Google respondeu certo, ou o gateway resolveu por dentro (retry/onda/
cooldown) e entregou a resposta. O harness distingue:
  - ok              : 200 com resposta completa, sem falha de backend
  - handled         : 200 com resposta completa, MAS o backend falhou antes
                      (429/503) e o gateway mascou — o sistema fazendo o certo
  - surfaced_error  : o cliente recebeu 429/503/502 — FALHA do objetivo 2
  - truncated       : stream 200 que nao terminou em [DONE] — FALHA oculta
  - timeout         : nao completou dentro do prazo — FALHA

Modo unico (exige --yes-real): aponta para o Google de verdade com as chaves
reais. Mede QUANTO o Google devolve e QUANTO TEMPO demora de fato. Consome
cota real — por isso exige confirmacao explicita.

Uso:
  python teste\\benchmark.py --yes-real --turns 10 --model gemini-3.8-flash
"""

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Tamanhos de contexto (chars -> est ~len//3 apos a calibracao):
# normal ~133k (cabe folgado), grande ~213k (enche o balde), oversized ~266k
# (>teto 250k: so entra em balde pristino — o regime real do usuario).
TAMANHOS = {"normal": 400_000, "grande": 640_000, "oversized": 800_000}

# Mensagens que o cliente NUNCA pode ver (regressao das duas dores reais):
# "cota de RPM" (balde cheio devolvido como erro em vez de estacionar) e
# "todas as chaves estao cheias" (deadlock de admissao em oversized).
MENSAGENS_PROIBIDAS = ("cota de RPM", "todas as chaves estao cheias")


def _corpo(modelo, stream, tamanho="grande"):
    corpo = {
        "model": modelo,
        "messages": [{"role": "user", "content": "x" * TAMANHOS.get(tamanho, TAMANHOS["grande"])}],
    }
    if stream:
        corpo["stream"] = True
    return corpo


def _parse_stream(texto):
    """Varre um corpo SSE: retorna (tem_done, tem_erro, tokens_out)."""
    tem_done = tem_erro = False
    tokens_out = 0
    for bloco in texto.split("\n\n"):
        bloco = bloco.strip()
        if not bloco.startswith("data:"):
            continue
        payload = bloco[len("data:"):].strip()
        if payload == "[DONE]":
            tem_done = True
            continue
        try:
            obj = json.loads(payload)
        except ValueError:
            continue
        if isinstance(obj, dict) and "error" in obj:
            tem_erro = True
        uso = obj.get("usage") if isinstance(obj, dict) else None
        if isinstance(uso, dict):
            tokens_out = int(uso.get("completion_tokens") or 0)
    return tem_done, tem_erro, tokens_out


def _classificar(status, stream, texto, wall, prazo):
    """Da resposta HTTP ao cliente para a categoria do objetivo 2."""
    if wall > prazo:
        return "timeout", 0
    if any(m in (texto or "") for m in MENSAGENS_PROIBIDAS):
        return "mensagem_proibida", 0
    if status != 200:
        return "surfaced_error", 0
    if stream:
        tem_done, tem_erro, tokens_out = _parse_stream(texto)
        if tem_erro:
            return "surfaced_error", tokens_out
        if not tem_done:
            return "truncated", tokens_out
        return "ok", tokens_out
    try:
        obj = json.loads(texto) if texto else {}
    except ValueError:
        obj = {}
    tokens_out = int(((obj.get("usage") or {}).get("completion_tokens")) or 0)
    if isinstance(obj, dict) and "error" in obj:
        return "surfaced_error", tokens_out
    return "ok", tokens_out


def _rodar_turno(cliente, modelo, stream, prazo, tamanho="grande"):
    corpo = _corpo(modelo, stream, tamanho)
    t0 = time.time()
    try:
        resp = cliente.post("/v1/chat/completions", json=corpo, timeout=prazo + 15)
        wall = time.time() - t0
        status = resp.status_code
        texto = resp.text
    except Exception as erro:  # noqa: BLE001 - o harness registra, nao engole
        wall = time.time() - t0
        return {"classe": "surfaced_error", "status": 0, "wall": wall,
                "tokens_out": 0, "tamanho": tamanho,
                "erro": str(erro)[:120]}
    classe, tokens_out = _classificar(status, stream, texto, wall, prazo)
    return {"classe": classe, "status": status, "wall": wall,
            "tokens_out": tokens_out, "tamanho": tamanho, "erro": None}


def _estatisticas(db_path, id_min):
    """Sondagem do backend: tentativas com id > id_min (sem dupla-contagem
    entre rajadas — timestamp de 1s compartilhava o segundo da virada)."""
    out = Counter()
    try:
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        for r in con.execute(
            "SELECT status, tokens_in, tokens_out FROM requisicoes WHERE id > ?",
            (id_min,),
        ):
            st = r["status"]
            out["tentativas"] += 1
            if st == 200:
                out["backend_ok"] += 1
            elif st == 429:
                out["backend_429"] += 1
            elif st == 503:
                out["backend_503"] += 1
            elif st:
                out["backend_erro"] += 1
        con.close()
    except sqlite3.Error:
        pass
    return out


# --------------------------------------------------------------------------
# Montagem dos modos
# --------------------------------------------------------------------------

def _montar_real(modelo, waves):
    """Google de verdade com as chaves reais. CONSOME COTA."""
    cfg_real = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    cfg_real["modelos"] = [modelo] + [m for m in cfg_real.get("modelos", []) if m != modelo]
    if waves:
        cfg_real["onda_limite_tokens"] = 100000  # p/ pedidos de ~160k entrarem no pacing
    os.environ.pop("GEMINI_API_BASE", None)  # usa o endpoint real padrao
    return None, cfg_real, modelo


# --------------------------------------------------------------------------

def principal():
    ap = argparse.ArgumentParser(description="Benchmark dos 2 objetivos do gateway")
    ap.add_argument("--turns", type=int, default=10, help="mensagens na rajada (default 10)")
    ap.add_argument("--window", type=float, default=60.0, help="janela de 1 minuto (default 60s)")
    ap.add_argument("--stream", action="store_true", help="usa streaming (como o opencode)")
    ap.add_argument("--yes-real", action="store_true",
                    help="confirmacao obrigatoria: consome cota real do Google")
    ap.add_argument("--model", default="gemini-3.8-flash", help="modelo a usar")
    ap.add_argument("--waves", action="store_true", help="liga o pacing de ondas")
    ap.add_argument("--bursts", type=int, default=1,
                    help="n. de rajadas seguidas (a 1a semeia episodio p/ hedge da 2a)")
    args = ap.parse_args()

    if not args.yes_real:
        custo = args.turns * 188000
        print(f"o benchmark consome ~{custo/1e6:.1f}M tokens de cota REAL ({args.turns} pedidos de ~188k).")
        print("Rode com --yes-real para confirmar. Nada foi executado.")
        return 2

    pasta = Path(tempfile.mkdtemp(prefix="bench-"))
    cfg_path = pasta / "config.json"
    db_path = pasta / "bench.db"
    os.environ["MONITOR_CONFIG"] = str(cfg_path)
    os.environ["MONITOR_DB"] = str(db_path)

    _, cfg, modelo = _montar_real(args.model, args.waves)
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    from fastapi.testclient import TestClient
    from app.main import criar_app

    app = criar_app()
    cliente = TestClient(app)
    with cliente:
        app.state.agendador.parar()  # congelar poller p/ medicao deterministica

        print("=" * 68)
        print(f"BENCHMARK  modo=real  bursts={args.bursts}x{args.turns}  window={args.window:.0f}s  "
              f"stream={args.stream}")
        print(f"modelo={modelo}  chaves={len(cfg['chaves'])}  contexto=~188k tokens/pedido")
        print("=" * 68)

        tudo = []
        marca0 = _max_id(db_path)
        # Sessao realista com contexto CRESCENTE (igual opencode ao longo
        # do dia): normais -> grandes -> oversized (>teto, regime do bug).
        # Cada rajada alinhada a minuto fresco: mede 10/min SUSTENTADOS
        # (40 espremidos num minuto testaria sobrecarga, nao o objetivo).
        planos = [
            ["normal"] * args.turns,
            ["normal"] * (args.turns // 2) + ["grande"] * (args.turns - args.turns // 2),
            ["grande"] * (args.turns // 2) + ["oversized"] * (args.turns - args.turns // 2),
            ["oversized"] * args.turns,
        ]
        n_rajadas = max(1, args.bursts)
        for b in range(n_rajadas):
            plano = planos[b % len(planos)]
            marca = _max_id(db_path)
            t0 = time.time()
            resultados = []
            with ThreadPoolExecutor(max_workers=args.turns) as ex:
                futs = [ex.submit(_rodar_turno, cliente, modelo, args.stream, args.window, tam)
                        for tam in plano[:args.turns]]
                for f in as_completed(futs):
                    resultados.append(f.result())
            parede = time.time() - t0
            print()
            print(f"----- RAJADA {b+1} [{'+'.join(sorted(set(plano[:args.turns])))}] -----")
            _relatorio(args, resultados, parede, db_path, marca)
            tudo.extend(resultados)

        if max(1, args.bursts) > 1:
            print()
            print("===== COMBINADO (%d rajadas x %d) =====" % (args.bursts, args.turns))
            _relatorio(args, tudo, sum(r["wall"] for r in tudo), db_path, marca0)
    return 0


def _max_id(db_path):
    """Maior id em requisicoes (0 se tabela vazia/ausente)."""
    try:
        con = sqlite3.connect(db_path)
        n = con.execute("SELECT MAX(id) FROM requisicoes").fetchone()[0]
        con.close()
        return int(n or 0)
    except sqlite3.Error:
        return 0


def _relatorio(args, resultados, parede, db_path, id_min):
    classes = Counter(r["classe"] for r in resultados)
    walls = sorted(r["wall"] for r in resultados)
    tokens_out = sum(r["tokens_out"] for r in resultados)
    dentro = sum(1 for r in resultados if r["classe"] in ("ok", "handled") and r["wall"] <= args.window)

    # handled = 200 limpo ao cliente, mas o backend falhou antes (mascarado)
    backend = _estatisticas(db_path, id_min)
    falhas_backend = backend["backend_429"] + backend["backend_503"] + backend["backend_erro"]
    entregues = classes["ok"] + classes["handled"]
    # aproximacao agregada: quantos dos entregues passaram por falha de backend
    handled_aprox = min(entregues, max(0, falhas_backend)) if falhas_backend else 0
    ok_limpo = entregues - handled_aprox

    def pct(n, d):
        return f"{100.0*n/d:.0f}%" if d else "n/a"

    print()
    print("OBJETIVO 1 — Throughput (ate %d mensagens completas em %.0fs)" % (args.turns, args.window))
    print("  completas dentro da janela : %d/%d  (%s)" % (dentro, args.turns, pct(dentro, args.turns)))
    if walls:
        p50 = walls[len(walls)//2]
        p95 = walls[min(len(walls)-1, int(len(walls)*0.95))]
        print("  tempo de parede por pedido : p50=%.1fs  p95=%.1fs  max=%.1fs" % (p50, p95, walls[-1]))
    print("  tokens devolvidos (out)    : %d (media %.0f/pedido)" % (tokens_out, tokens_out/max(1, args.turns)))
    print("  parede total da rajada     : %.1fs" % parede)
    veredito1 = "PASS" if dentro >= args.turns else "FAIL"
    print("  -> OBJETIVO 1: %s" % veredito1)

    print()
    print("OBJETIVO 2 — Zero erro visivel ao usuario")
    print("  ok (200 limpo)             : %d" % ok_limpo)
    print("  handled (erro mascado)     : %d   <- o sistema resolveu por dentro" % handled_aprox)
    print("  surfaced_error (cliente viu): %d   <- FALHA" % classes["surfaced_error"])
    print("  truncated (stream cortado) : %d   <- FALHA oculta" % classes["truncated"])
    print("  timeout                    : %d   <- FALHA" % classes["timeout"])
    print("  mensagem_proibida          : %d   <- FALHA GRAVE ('cota de RPM'/'todas as chaves...')" % classes["mensagem_proibida"])
    visiveis = (classes["surfaced_error"] + classes["truncated"] + classes["timeout"]
                + classes["mensagem_proibida"])
    veredito2 = "PASS" if visiveis == 0 else "FAIL"
    print("  -> OBJETIVO 2: %s (%d erros visiveis)" % (veredito2, visiveis))

    print()
    print("SONDAGEM DO BACKEND (o que o Google realmente viu):")
    print("  tentativas : %d | ok : %d | 429 : %d | 503 : %d | outros : %d" % (
        backend["tentativas"], backend["backend_ok"], backend["backend_429"],
        backend["backend_503"], backend["backend_erro"]))
    print("  falhas de backend que o gateway precisou absorver : %d" % falhas_backend)
    print()
    print("=" * 68)
    print("RESULTADO FINAL: objetivo1=%s  objetivo2=%s" % (veredito1, veredito2))
    print("=" * 68)


if __name__ == "__main__":
    raise SystemExit(principal())
