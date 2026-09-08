#!/usr/bin/env python3
import argparse
import os
import sys
import time
from datetime import datetime, timezone

from app import config, db, gemini_api, janelas
from app.estado import Estado

NOMES = {"minuto": "minuto RPM/TPM", "dia": "dia RPD", "mes": "mes tokens"}


def fmt_delta(seg):
    if seg is None:
        return "?"
    h, m = divmod(seg // 60, 60)
    if h:
        return f"{h}H {m:02d}M"
    return f"{m}M {seg % 60:02d}S"


def fmt_percent(p):
    if p is None:
        return "--"
    return f"{float(p):.0f}%"


def segundos_ate(iso):
    if not iso:
        return None
    try:
        fim = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if fim.tzinfo is None:
            fim = fim.replace(tzinfo=timezone.utc)
        return max(0, int((fim - datetime.now(timezone.utc)).total_seconds()))
    except ValueError:
        return None


def barra(percent, largura=28):
    if percent is None:
        return "[sem dados]"
    frac = max(0.0, min(1.0, float(percent) / 100.0))
    cheio = int(frac * largura)
    return "[" + "#" * cheio + " " * (largura - cheio) + f"] {fmt_percent(percent)}"


def coletar(chaves, estado, cfg):
    resultados = []
    for chave in chaves:
        ok, resp = gemini_api.validar_chave(chave["key"])
        if not ok:
            estado.marcar_erro(chave["nome"], resp)
            resultados.append((chave["nome"], None, resp))
            continue
        limites = janelas.limites_por_chave(cfg, chave)
        uso = db.uso_bruto(chave["nome"])
        jan = janelas.calcular(uso, limites)
        estado.marcar_uso(chave["nome"], jan)
        db.registrar_snapshot(chave["nome"], jan, {"uso": uso, "limites": limites})
        resultados.append((chave["nome"], jan, None))
    return resultados


def mostrar(resultados, limiar):
    print("Google Gemini - uso das chaves (local)")
    print("-" * 56)
    pior_geral = 0.0
    for nome, janelas, erro in resultados:
        if erro:
            print(f"{nome:>10}: ERRO - {erro}")
            continue
        linha = [f"{nome:>10}:", ""]
        for jan_nome in ("minuto", "dia", "mes"):
            jan = janelas.get(jan_nome) or {}
            pct = jan.get("percent")
            if pct is not None:
                pior_geral = max(pior_geral, float(pct))
            status = "OK"
            if pct is not None and float(pct) >= limiar:
                status = "ALERTA"
            if pct is not None and float(pct) >= 95:
                status = "CRITICO"
            linha.append(f"{NOMES[jan_nome]} {fmt_percent(pct)} [{status}]")
        print(" | ".join(linha))
    print("-" * 56)
    print(f"Janela mais cheia entre todas as chaves: {fmt_percent(pior_geral)}  (limiar: {limiar}%)")


def mostrar_historico(limite):
    db.inicializar()
    linhas = db.historico_uso(limite=limite)
    if not linhas:
        print("historico vazio.")
        return
    print("Ultimas coletas:")
    print(f"{'quando':<22} {'chave':<12} {'min':>6} {'dia':>6} {'mes':>6}")
    for l in reversed(linhas):
        fmt = lambda v: f"{float(v):.0f}" if v is not None else "--"
        print(
            f"{l['quando']:<22} {l['chave']:<12} {fmt(l['minuto_pct']):>6} {fmt(l['dia_pct']):>6} {fmt(l['mes_pct']):>6}"
        )


def principal():
    parser = argparse.ArgumentParser(description="Monitora o uso das chaves da Gemini API (local)")
    parser.add_argument("--chave", help="key avulsa (ou env GEMINI_API_KEY / GOOGLE_API_KEY)")
    parser.add_argument("--limiar", type=float, help="percentual de alerta (default: config.json)")
    parser.add_argument("--watch", type=int, nargs="?", const=-1, metavar="SEG", help="fica monitorando a cada SEG")
    parser.add_argument("--historico", type=int, nargs="?", const=10, metavar="N", help="mostra as ultimas N coletas")
    parser.add_argument("--silencioso", action="store_true", help="no modo watch so imprime quando muda")
    args = parser.parse_args()

    cfg = config.carregar()
    if args.chave:
        cfg["chaves"] = [{"nome": "chave-cli", "key": args.chave, "tipo": "gemini"}]
    elif os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        cfg["chaves"] = [
            {"nome": "chave-env", "key": os.environ.get("GEMINI_API_KEY") or os.environ["GOOGLE_API_KEY"], "tipo": "gemini"}
        ]
    cfg["chaves"] = config.chaves_validas(cfg)
    if not cfg["chaves"]:
        print("Sem key configurada. Edite config.json (lista \"chaves\") ou use --chave.")
        sys.exit(1)
    limiar = args.limiar if args.limiar is not None else float(cfg.get("limiar_alerta", 80))
    db.inicializar()

    if args.historico:
        mostrar_historico(args.historico)
        return

    estado = Estado()
    if args.watch is None:
        mostrar(coletar(cfg["chaves"], estado, cfg), limiar)
        return

    intervalo = args.watch if args.watch and args.watch > 0 else int(cfg.get("intervalo_uso_seg", 60))
    print(f"Monitorando a cada {intervalo}s - Ctrl+C para parar")
    ultimo = None
    try:
        while True:
            resultados = coletar(cfg["chaves"], estado, cfg)
            chave_estado = tuple(
                tuple((janelas or {}).get(n) or {} for n in ("minuto", "dia", "mes"))
                if janelas
                else None
                for _nome, janelas, _erro in resultados
            )
            if args.silencioso and chave_estado == ultimo:
                time.sleep(intervalo)
                continue
            ultimo = chave_estado
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}]")
            mostrar(resultados, limiar)
            time.sleep(intervalo)
    except KeyboardInterrupt:
        print("\nparado.")


if __name__ == "__main__":
    principal()
