"""Medidor de KPIs do gateway — prova objetiva de que as correcoes funcionaram.

Uso:
    python teste\\analise_kpis.py [saida.json]

Reconstrui "cadeias" (uma requisicao do cliente = 1..N tentativas internas)
a partir do historico.db usando a mesma tecnica dos relatorios anteriores:
cada linha tem inicio = quando - ms; linhas com inicio quase igual pertencem
a mesma cadeia. Somente leitura; nao altera nada no gateway.
"""

import json
import re
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "historico.db"
TOLERANCIA_CADEIA_S = 3.0


def _parse(q):
    return datetime.fromisoformat(q)


def carregar():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, quando, chave, ok, status, ms, erro FROM requisicoes ORDER BY id"
    ).fetchall()
    con.close()
    return rows


def montar_cadeias(rows):
    cadeias, cur = [], []
    for r in rows:
        ini = _parse(r["quando"]) - timedelta(milliseconds=r["ms"] or 0)
        if cur and abs((ini - cur[-1][0]).total_seconds()) > TOLERANCIA_CADEIA_S:
            cadeias.append(cur)
            cur = []
        cur.append((ini, r))
    if cur:
        cadeias.append(cur)
    for c in cadeias:
        c.sort(key=lambda x: x[1]["id"])
    return cadeias


def kpi_cadeias(cadeias):
    durs, tentas = [], []
    ok_end = fail_end = 0
    saudavel = desperdicio = 0.0
    peso_morto_ok = []
    lat_ok = []
    por_causa = {}
    for ch in cadeias:
        ms = [r["ms"] or 0 for _, r in ch]
        n = len(ch)
        durs.append(ms[-1] / 1000)
        tentas.append(n)
        inter = sum(max(ms[i] - (ms[i - 1] if i else 0), 0) for i in range(n - 1)) if n > 1 else 0
        final = max(ms[-1] - (ms[-2] if n > 1 else 0), 0)
        if ch[-1][1]["ok"]:
            ok_end += 1
            lat_ok.append(ms[-1] / 1000)
            peso_morto_ok.append(inter / 1000)
            desperdicio += inter
            saudavel += final
        else:
            fail_end += 1
            desperdicio += inter + final
            primeiro = ch[0][1]
            chave_causa = f"{primeiro['status']} {(primeiro['erro'] or '')[:40]}"
            por_causa[chave_causa] = por_causa.get(chave_causa, 0) + inter + final
    durs.sort()
    lat_ok.sort()
    total = saudavel + desperdicio
    return {
        "cadeias_total": len(cadeias),
        "cadeias_ok_pct": round(100 * ok_end / max(len(cadeias), 1), 1),
        "duracao_s": {"media": round(statistics.mean(durs), 1), "mediana": round(statistics.median(durs), 1),
                      "p90": round(durs[int(len(durs) * .9)], 1), "max": round(durs[-1], 1)},
        "tentativas_max": max(tentas),
        "tentativas_gt10_pct": round(100 * sum(1 for t in tentas if t > 10) / len(tentas), 2),
        "tempo_inutil_pct": round(100 * desperdicio / max(total, 1), 1),
        "desperdicio_por_causa_h": {
            k: round(v / 1000 / 3600, 2)
            for k, v in sorted(por_causa.items(), key=lambda x: -x[1])[:6]
        },
        "peso_morto_medio_em_ok_s": round(statistics.mean(peso_morto_ok), 1) if peso_morto_ok else 0,
        "latencia_ok_s": {"mediana": round(statistics.median(lat_ok), 1) if lat_ok else None,
                          "p90": round(lat_ok[int(len(lat_ok) * .9)], 1) if len(lat_ok) > 10 else None},
    }


def kpi_over_cooldown(rows):
    """Excesso de espera: par (mesma chave, 429 com 'retry in Xs', proxima
    tentativa nessa chave). gap = intervalo ate a proxima tentativa. O gateway
    ideal tem excesso ~0 (gap ~= X). Valores altos = punicao inventada."""
    excessos, razoes = [], []
    ultima_429 = {}
    for r in rows:
        k, t = r["chave"], _parse(r["quando"]).timestamp()
        ant = ultima_429.pop(k, None)
        if ant and r["ok"] == 0 or (ant and r["id"] != ant["id"]):
            gap = t - ant["t"]
            if 0 < gap < 3600:
                excessos.append(gap - ant["delay"])
                razoes.append(gap / max(ant["delay"], 0.1))
        m = re.search(r"[Rr]etry in ([0-9.]+)s", r["erro"] or "")
        if r["status"] == 429 and m:
            ultima_429[k] = {"t": t, "delay": float(m.group(1)), "id": r["id"]}
    if not excessos:
        return {"amostras": 0}
    excessos.sort()
    razoes.sort()
    return {
        "amostras": len(excessos),
        "excesso_mediano_s": round(statistics.median(excessos), 1),
        "razao_mediana_imposto_vs_pedido": round(statistics.median(razoes), 1),
        "p90_excesso_s": round(excessos[int(len(excessos) * .9)], 1),
    }


def kpi_503_repetido(rows):
    """% de 503 seguidos de nova tentativa na MESMA chave em <=10s.
    Com cooldown curto de 10s isso deve ir a ~0."""
    ultimas = {}
    repetidas = total = 0
    for r in rows:
        k, t = r["chave"], _parse(r["quando"]).timestamp()
        ant = ultimas.get(k)
        if r["status"] == 503:
            total += 1
            ultimas[k] = ("503", t)
        elif ant and ant[0] == "503" and t - ant[1] <= 10:
            repetidas += 1
            ultimas[k] = ("outro", t)
        else:
            ultimas[k] = ("outro", t)
    return {"total_503": total, "repetida_mesma_chave_10s": repetidas,
            "pct": round(100 * repetidas / max(total, 1), 1)}


def main():
    rows = carregar()
    cadeias = montar_cadeias(rows)
    kpis = {
        "janela": {"primeira": rows[0]["quando"], "ultima": rows[-1]["quando"], "linhas": len(rows)},
        "cadeias": kpi_cadeias(cadeias),
        "over_cooldown": kpi_over_cooldown(rows),
        "503_repetido": kpi_503_repetido(rows),
    }
    print(json.dumps(kpis, indent=2, ensure_ascii=False))
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(json.dumps(kpis, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nsalvo em {sys.argv[1]}")


if __name__ == "__main__":
    main()
