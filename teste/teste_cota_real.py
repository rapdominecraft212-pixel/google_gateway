"""Teste controlado REAL: cota por chave, independencia entre chaves, throttle por modelo.

Responde com o Google de verdade (zero mock, funcao de producao abrir_chat):
  P0  radiografia das 36 chaves AGORA (1 pedido por chave, 3.8-flash)
  P1  quantas reqs/min UMA chave aguenta antes do 429 (martela 8x uma chave)
  P2  saturar a chave A contamina a chave B? (se sim, balde compartilhado)
  P3  a chave saturada ainda serve OUTRO modelo? (se sim, throttle e por modelo)
  P4  ritmo sustentavel da pool: 6 chaves x limite-medido em 60s

Custo: ~76 pedidos com max_tokens=1 e prompt de 1 palavra (~500 tokens).
Reporta o total no fim. Exige --yes-real (consome cota real).

Uso: python teste/teste_cota_real.py --yes-real
"""
import json
import sys
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from app import config as config_mod  # noqa: E402
from app import gemini_api  # noqa: E402

MODELO = "gemini-3.8-flash"
MODELO_CONTROLE = "gemini-3.5-flash-lite"
CAND = threading.Lock()
_gasto = {"reqs": 0, "tokens": 0}


def corpo(modelo):
    return json.dumps({
        "model": modelo,
        "messages": [{"role": "user", "content": "oi"}],
        "max_tokens": 1,
    }).encode("utf-8")


def disparar(chave, modelo=MODELO, timeout=60):
    """Um pedido real ao Google pela funcao de producao. Nunca levanta: devolve
    (status, erro_texto_completo, tokens_totais, ms)."""
    t0 = time.time()
    with CAND:
        _gasto["reqs"] += 1
    try:
        resp = gemini_api.abrir_chat(chave["key"], corpo(modelo), timeout=timeout, nome=chave["nome"])
        try:
            dados = json.loads(resp.read())
            tok = (dados.get("usage") or {}).get("total_tokens", 0)
        except Exception:
            tok = 0
        with CAND:
            _gasto["tokens"] += tok
        return resp.status, None, tok, int((time.time() - t0) * 1000)
    except urllib.error.HTTPError as erro:
        try:
            texto = erro.read().decode("utf-8", "replace")
        except Exception:
            texto = ""
        return erro.code, texto[:400], 0, int((time.time() - t0) * 1000)
    except Exception as erro:
        return None, f"{type(erro).__name__}: {erro}", 0, int((time.time() - t0) * 1000)


def por_chave(cfg):
    return [c for c in config_mod.chaves_validas(cfg) if c.get("ativa") is not False]


def p0(chaves):
    print("=" * 72)
    print("P0 — radiografia das 36 chaves AGORA (1 pedido cada, 3.8-flash)")
    resultado = {}

    def um(c):
        st, err, tok, ms = disparar(c)
        resultado[c["nome"]] = (st, err, ms)
        return c["nome"], st, ms

    with ThreadPoolExecutor(max_workers=6) as ex:
        for nome, st, ms in ex.map(um, chaves):
            marca = "OK " if st == 200 else ("503" if st == 503 else ("429" if st == 429 else str(st)))
            print(f"  {nome:<11} {marca} {ms}ms")
    ok = [n for n, (st, _, _) in resultado.items() if st == 200]
    e429 = [n for n, (st, _, _) in resultado.items() if st == 429]
    e503 = [n for n, (st, _, _) in resultado.items() if st == 503]
    print(f"\n  P0: {len(ok)} ok | {len(e429)} x429 | {len(e503)} x503 | outros: {len(resultado)-len(ok)-len(e429)-len(e503)}")
    for n in e429[:3]:
        print(f"    texto 429 ({n}): {resultado[n][1][:200]}")
    return resultado, ok


def p1(chaves, ok_p0):
    print("=" * 72)
    print("P1 — limite real POR CHAVE: 8 pedidos seguidos numa chave sadia")
    alvo = next(c for c in chaves if c["nome"] == ok_p0[0])
    print(f"  chave martelada: {alvo['nome']} (ela ja fez 1 pedido no P0)")
    primeira_429 = None
    for i in range(1, 9):
        st, err, tok, ms = disparar(alvo)
        marca = "OK " if st == 200 else st
        print(f"  tentativa {i}: {marca} {ms}ms")
        if st == 429 and primeira_429 is None:
            primeira_429 = i
            print(f"    TEXTO COMPLETO: {err}")
        time.sleep(0.4)
    print(f"\n  P1: primeira recusa na tentativa {primeira_429} (com o pedido do P0 na janela: "
          f"limite real ~ {(primeira_429 - 1) + 1 if primeira_429 else '>8'} req/min/chave)")
    return alvo, primeira_429


def p2(chaves, alvo_p1, ok_p0):
    print("=" * 72)
    print("P2 — independencia: chave B parada desde o P0, agora 1 pedido")
    b = next(c for c in chaves if c["nome"] in ok_p0 and c["nome"] != alvo_p1["nome"])
    st, err, tok, ms = disparar(b)
    print(f"  {b['nome']}: {'OK' if st == 200 else st} {ms}ms")
    if st == 200:
        print("  => B passou com A saturada: baldes INDEPENDENTES por chave")
    elif st == 429:
        print(f"  => B recusou junto: balde COMPARTILHADO ou throttle do modelo. Texto: {err[:200]}")
    else:
        print(f"  => B deu {st} (capacidade do modelo, nao quota)")
    return b, st


def p3(alvo_p1):
    print("=" * 72)
    print("P3 — a chave saturada do 3.8-flash ainda serve outro modelo?")
    st, err, tok, ms = disparar(alvo_p1, MODELO_CONTROLE)
    print(f"  {alvo_p1['nome']} em {MODELO_CONTROLE}: {'OK' if st == 200 else st} {ms}ms")
    if st == 200:
        print("  => passou: o bloqueio e POR MODELO (3.8-flash), nao pela chave")
    elif st == 429:
        print(f"  => recusou tambem: bloqueio e da CHAVE/conta inteira. Texto: {err[:200]}")
    return st


def p4(chaves, ok_p0, alvo_p1, limite):
    print("=" * 72)
    n_chaves = 6
    cada = max(1, min(5, limite or 5))
    nomes = [n for n in ok_p0 if n != alvo_p1["nome"]][:n_chaves]
    print(f"P4 — ritmo sustentavel: {n_chaves} chaves x {cada} pedidos em ~60s ({MODELO})")
    alvos = [c for c in chaves if c["nome"] in nomes]
    ok = 0
    recusa = 0
    t0 = time.time()
    for rodada in range(cada):
        for c in alvos:
            st, err, tok, ms = disparar(c)
            if st == 200:
                ok += 1
            else:
                recusa += 1
                if recusa <= 3:
                    print(f"    recusa {c['nome']} rodada {rodada+1}: {st} {str(err)[:150]}")
        tempo = time.time() - t0
        print(f"  rodada {rodada+1}/{cada}: {ok} ok, {recusa} recusas, {tempo:.0f}s decorridos")
        time.sleep(max(0.0, 60.0 / cada - (time.time() - t0) % (60.0 / cada)))
    dur = time.time() - t0
    print(f"\n  P4: {ok}/{ok+recusa} sucesso em {dur:.0f}s = {ok/(dur/60):.1f} req/min sustentados")


def main():
    if "--yes-real" not in sys.argv:
        print("Precisa de --yes-real: este teste faz ~76 pedidos REAIS ao Google (~500 tokens).")
        sys.exit(1)
    cfg = config_mod.carregar()
    chaves = por_chave(cfg)
    print(f"{len(chaves)} chaves ativas. Modelo: {MODELO}. Hora: {time.strftime('%H:%M:%S')}\n")
    t0 = time.time()
    res_p0, ok_p0 = p0(chaves)
    if not ok_p0:
        print("\nNENHUMA chave passou no P0 — tempestade em curso; P1+ mediria a tempestade, nao a cota.")
        print("Mesmo assim rodando P3 com a primeira chave para ver o texto do erro...")
        st = p3(chaves[0])
        print(f"\nCusto: {_gasto['reqs']} pedidos, ~{_gasto['tokens']} tokens, {time.time()-t0:.0f}s")
        return
    alvo, primeira_429 = p1(chaves, ok_p0)
    limite = (primeira_429 - 1) + 1 if primeira_429 else 9
    p2(chaves, alvo, ok_p0)
    p3(alvo)
    p4(chaves, ok_p0, alvo, limite)
    print("=" * 72)
    print(f"CUSTO TOTAL: {_gasto['reqs']} pedidos reais, ~{_gasto['tokens']} tokens, {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
