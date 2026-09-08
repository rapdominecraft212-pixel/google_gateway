#!/usr/bin/env python3
"""Le a trilha do dia: reconstrói, pedido a pedido, o que aconteceu e POR QUE.

O gateway grava tudo em logs/eventos-diagnostico.log (eventos pedido_*):
cada pedido do cliente tem um req_id que amarra inicio -> rodadas (quais
chaves, por que essas) -> cada falha (o que o Google disse, cooldown aplicado,
quanto do balde RPM a chave já tinha usado NA HORA) -> esperas -> desfecho.

Uso:
    python teste/ler_trilha.py --desde 18:15 --ate 18:25
    python teste/ler_trilha.py --desde 18:15 --req a1b2c3
    python teste/ler_trilha.py --desde 18:15 --veredito   (só o resumo do porquê)

Leitura local, zero cota, zero rede.
"""
import argparse
import collections
import json
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
LOG = RAIZ / "logs" / "eventos-diagnostico.log"


def carregar(desde, ate):
    evs = []
    try:
        linhas = LOG.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return evs
    for ln in linhas:
        try:
            o = json.loads(ln)
        except ValueError:
            continue
        if not str(o.get("evento", "")).startswith("pedido_"):
            continue
        hhmm = str(o.get("ts", ""))[11:16]
        if desde and hhmm < desde:
            continue
        if ate and hhmm > ate:
            continue
        evs.append(o)
    return evs


def agrupar(evs):
    pedidos = collections.OrderedDict()
    for o in evs:
        req = (o.get("detalhe") or {}).get("req") or "?"
        pedidos.setdefault(req, []).append(o)
    return pedidos


def mostrar_pedido(req, evs):
    d0 = (evs[0].get("detalhe") or {})
    print(f"\n=== pedido {req} ===")
    for o in evs:
        d = o.get("detalhe") or {}
        ts = str(o.get("ts", ""))[11:23]
        ev = o.get("evento", "")
        if ev == "pedido_inicio":
            print(f"  {ts} INICIO modelo={d.get('modelo')} tokens_est={d.get('tokens_est')} "
                  f"racers_base={d.get('racers_base')} alvo_ma={d.get('alvo_ma')}")
        elif ev == "pedido_rodada":
            print(f"  {ts} rodada {d.get('rodada')}: {_n(d)} chave(s) "
                  f"{(d.get('chaves') or [])} | eleicao: {d.get('razao')}")
        elif ev == "pedido_falha":
            extra = ""
            if d.get("quota_real"):
                extra = f"QUOTA-REAL retry_google={d.get('retry_google_s')}s"
            elif d.get("tipo") == "capacidade":
                extra = "capacidade"
            print(f"  {ts} FALHA {d.get('chave')} [{d.get('tipo')}] "
                  f"st={d.get('status')} cd={d.get('cooldown_s')}s "
                  f"balde_chave={d.get('reqs_60s')}req/{d.get('tokens_60s')}tok {extra}")
            print(f"         google: {(d.get('erro_google') or '')[:130]}")
        elif ev == "pedido_espera":
            print(f"  {ts} ESPERA {d.get('segundos')}s motivo={d.get('motivo')}")
        elif ev == "pedido_fim":
            print(f"  {ts} FIM saida={d.get('saida')} cliente={d.get('status_cliente')} "
                  f"total={d.get('ms_total')}ms queimadas={d.get('chaves_queimadas')} "
                  f"{d.get('detalhe', '')[:120]}")


def _n(d):
    return len(d.get("chaves") or [])


def veredito(pedidos, rpm_cfg=20):
    tot = len(pedidos)
    saidas = collections.Counter()
    quota_reais = []
    cap_por_min = collections.Counter()
    pico_chave_min = collections.Counter()
    for req, evs in pedidos.items():
        for o in evs:
            d = o.get("detalhe") or {}
            ev = o.get("evento", "")
            if ev == "pedido_fim":
                saidas[d.get("saida")] += 1
            elif ev == "pedido_falha":
                mm = str(o.get("ts", ""))[11:16]
                if d.get("quota_real"):
                    quota_reais.append((mm, d.get("chave"), d.get("retry_google_s")))
                if d.get("tipo") == "capacidade":
                    cap_por_min[mm] += 1
                pico_chave_min[(mm, d.get("chave"))] += 1
    print(f"\n######## VEREDITO ({tot} pedidos) ########")
    print(f"desfechos p/ o cliente: {dict(saidas)}")
    pico = max(pico_chave_min.values()) if pico_chave_min else 0
    print(f"pico de tentativas 1 chave em 1 min: {pico} (teto configurado rpm={rpm_cfg})")
    if pico <= rpm_cfg:
        print("  => nenhuma chave estourou o RPM local: recusas com balde sobrando = "
              "throttle do MODELO no Google, nao quota sua.")
    else:
        print("  => HOUVE estouro de RPM local: o gateway passou do teto — ver valvula/admissao.")
    if quota_reais:
        print(f"429 de QUOTA de verdade: {len(quota_reais)} (ex.: {quota_reais[:3]})")
        print("  => esses sim sao limite RPM/TPM estourado; 'retry_google_s' diz quando abre.")
    else:
        print("429 de QUOTA de verdade: 0 — todo 429 do periodo foi generico (capacidade).")
    if cap_por_min:
        top = sorted(cap_por_min.items(), key=lambda kv: -kv[1])[:5]
        print(f"minutos de tempestade (falhas de capacidade): {top}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--desde", default=None, help="HH:MM")
    ap.add_argument("--ate", default=None, help="HH:MM")
    ap.add_argument("--req", default=None)
    ap.add_argument("--veredito", action="store_true")
    ap.add_argument("--rpm", type=int, default=20)
    args = ap.parse_args()
    evs = carregar(args.desde, args.ate)
    if not evs:
        print("sem eventos pedido_* no periodo (o gateway novo ja estava no ar?)")
        return
    pedidos = agrupar(evs)
    if args.req:
        evs_r = pedidos.get(args.req)
        if not evs_r:
            print(f"req {args.req} nao encontrado no periodo")
            return
        mostrar_pedido(args.req, evs_r)
        return
    if args.veredito:
        veredito(pedidos, args.rpm)
        return
    for req, evs_r in pedidos.items():
        mostrar_pedido(req, evs_r)
    veredito(pedidos, args.rpm)


if __name__ == "__main__":
    main()
