#!/usr/bin/env python3
"""Teste REAL da cota compartilhada por projeto (F1/F2 do plano).

Zero mock: cada caso chama as FUNCOES DE PRODUCAO (Valvula, Estado,
roteador._elegiveis/escolher) com dados reais e confere a saida real.

O que cada caso prova:
  T1  Valvula.configurar_grupo: UM balde de fichas para a frota inteira
      (3 disparos em 3 chaves DIFERENTES esgotam o balde do grupo)
  T2  Estado grupo TPM: uso na chave A aparece na folga da chave B "fria"
      (a mentira do bug B1 morre aqui: 96% dos 429 de producao chegaram
      com janela local zerada porque o balde era do PROJETO)
  T3  Reserva atomica contra o balde do grupo (corrida entre chaves)
  T4  Roteador: com cota_compartilhada, RPM do grupo esgotado = NINGUEM
      elegivel (antes: chave fria era vista com folga inexistente)
  T5  Roteador: admissao preditiva de TPM pelo grupo (pedido grande
      recusado quando o balde comum nao comporta)
  T6  marcar_cooldown_grupo: UM 429 de quota trava a frota inteira (F2)
      - e nao encurta bloqueio maior ja existente
  T7  Modo desligado (cota_compartilhada: false): comportamento por chave
      classico preservado (regressao zero quando o flag nao esta ligado)

Uso: python teste/teste_cota_compartilhada.py
Custo: zero (funcoes puras + estado em memoria; sem rede, sem banco).
"""
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from app.estado import Estado  # noqa: E402
from app.valvula import Valvula  # noqa: E402
from app import roteador  # noqa: E402

FALHAS = []


def caso(nome, cond, detalhe=""):
    marca = "OK " if cond else "FALHOU"
    print(f"  [{marca}] {nome}" + (f"  ({detalhe})" if detalhe and not cond else ""))
    if not cond:
        FALHAS.append(nome)


def cfg_base(compartilhada):
    return {
        "cota_compartilhada": compartilhada,
        "max_conc_por_key": 2,
        "limites": {"rpm": 2, "tpm": 1000, "requisicoes_dia": 1500,
                    "tokens_dia": 0, "tokens_mes": 20000000},
        "chaves": [
            {"nome": "A", "key": "k1", "tipo": "gemini", "ativa": True},
            {"nome": "B", "key": "k2", "tipo": "gemini", "ativa": True},
            {"nome": "C", "key": "k3", "tipo": "gemini", "ativa": True},
        ],
    }


def t1_valvula_grupo():
    print("=" * 72)
    print("T1 - Valvula.configurar_grupo: um balde de fichas para a frota")
    v = Valvula()
    v.configurar_grupo(["A", "B", "C"], 3)  # 3 fichas/min para o POLO
    ok = [v.consumir("A")[0], v.consumir("B")[0], v.consumir("C")[0]]
    caso("3 disparos em 3 chaves diferentes consomem o mesmo balde", all(ok))
    cheia, _ = v.consumir("A")
    caso("4o disparo (na chave A) bloqueia: balde do grupo esgotado", not cheia)
    v.devolver("C")  # recusa devolve a ficha ao balde COMUM
    vaga, _ = v.consumir("B")
    caso("devolver em C libera vaga vista por B (balde unico)", vaga)
    # re-configurar preserva as fichas (idempotente por pedido). Neste ponto
    # o balde tem ~0 fichas (3 iniciais + 1 devolvida - 4 consumidas).
    v.configurar_grupo(["A", "B", "C"], 3)
    v.configurar_grupo(["A", "B", "C"], 3)
    sobra = v._baldes["A"]["tokens"]
    caso("re-configurar o grupo NAO reseta as fichas", 0.0 <= sobra < 0.5,
         f"tokens={sobra}")


def t2_estado_grupo_tpm():
    print("=" * 72)
    print("T2 - Estado: uso na chave A aparece na folga da chave B (bug B1)")
    e = Estado()
    e.ativar_cota_grupo(["A", "B", "C"], teto_tpm=1000)
    e.registrar_uso_local("A", 600)
    disp_b = e.tpm_disponivel("B")  # B nunca foi usada: deve enxergar o grupo
    caso("tpm_disponivel(B) = 400 (1000 - 600 usados em A)", disp_b == 400,
         f"disp_b={disp_b}")
    caso("teto_de(B) = 1000 (teto do grupo)", e.teto_de("B") == 1000)
    caso("balde_pristino(B) = False (grupo ja usou)", e.balde_pristino("B") is False)
    reqs, tokens = e.uso_60s_grupo()
    caso("uso_60s_grupo = (1, 600)", (reqs, tokens) == (1, 600),
         f"({reqs}, {tokens})")


def t3_reserva_grupo():
    print("=" * 72)
    print("T3 - Reserva atomica contra o balde do grupo")
    e = Estado()
    e.ativar_cota_grupo(["A", "B", "C"], teto_tpm=1000)
    caso("reservar(A, 600) ok", e.reservar("A", 600) is True)
    caso("reservar(B, 500) recusado (sobram 400 do GRUPO)",
         e.reservar("B", 500) is False)
    caso("reservar(B, 400) ok (exatamente a folga)", e.reservar("B", 400) is True)
    caso("reservar(C, 1) recusado (balde do grupo cheio)",
         e.reservar("C", 1) is False)
    e.liberar("A", 600)
    # folga do grupo apos liberar A: 1000 - 400 (reserva de B) = 600
    caso("liberar(A, 600) devolve folga ao grupo",
         e.tpm_disponivel("C") == 600, f"disp={e.tpm_disponivel('C')}")
    # oversized em balde pristino do GRUPO: 1 aceito, 2o recusado
    e2 = Estado()
    e2.ativar_cota_grupo(["A", "B"], teto_tpm=1000)
    caso("oversized (1500) aceito UMA vez em balde pristino do grupo",
         e2.reservar("A", 1500) is True)
    caso("2o oversized (1500) recusado no mesmo balde",
         e2.reservar("B", 1500) is False)


def t4_roteador_rpm_grupo():
    print("=" * 72)
    print("T4 - Roteador: RPM do grupo esgotado = NINGUEM elegivel (F1)")
    cfg = cfg_base(compartilhada=True)
    e = Estado()
    e.ativar_cota_grupo(["A", "B", "C"], teto_tpm=1000)
    # A fez as 2 unicas reqs/min do projeto; B e C estao FRIAS
    e.registrar_uso_local("A", 10)
    e.registrar_uso_local("A", 10)
    agora = time.time()
    eleg = roteador._elegiveis(e, cfg, cfg["chaves"], 2, agora)
    caso("compartilhada: B e C frias TAMBEM ficam inelegiveis (balde comum)",
         eleg == [], f"elegiveis={[c['nome'] for c in eleg]}")
    # controle: mesmo estado sem o flag -> B e C elegiveis (comportamento antigo).
    # Nota: o filtro de RPM POR CHAVE (A fez 2 reqs) vive no proxy
    # (extra_cheia/rpm_cheia), nao no _elegiveis; aqui so importa que as
    # chaves FRIAS voltem a ser elegiveis quando o balde e por chave.
    cfg2 = cfg_base(compartilhada=False)
    e.desativar_cota_grupo()
    eleg2 = roteador._elegiveis(e, cfg2, cfg2["chaves"], 2, agora)
    nomes2 = {c["nome"] for c in eleg2}
    caso("sem o flag: B e C seguem elegiveis (regressao zero)",
         {"B", "C"}.issubset(nomes2), f"elegiveis={sorted(nomes2)}")


def t5_roteador_tpm_grupo():
    print("=" * 72)
    print("T5 - Roteador: admissao preditiva de TPM pelo grupo")
    cfg = cfg_base(compartilhada=True)
    e = Estado()
    e.ativar_cota_grupo(["A", "B", "C"], teto_tpm=1000)
    e.registrar_uso_local("A", 600)  # sobram 400 no balde do projeto
    agora = time.time()
    eleg = roteador._elegiveis(e, cfg, cfg["chaves"], 2, agora,
                               tokens_necessarios=500)
    caso("pedido de 500 tokens recusado no grupo inteiro (folga 400)",
         eleg == [], f"elegiveis={[c['nome'] for c in eleg]}")
    eleg2 = roteador._elegiveis(e, cfg, cfg["chaves"], 2, agora,
                                tokens_necessarios=300)
    caso("pedido de 300 tokens admitido (folga 400 comporta)",
         len(eleg2) > 0, f"elegiveis={[c['nome'] for c in eleg2]}")


def t6_cooldown_grupo():
    print("=" * 72)
    print("T6 - marcar_cooldown_grupo: um 429 trava a frota (F2)")
    e = Estado()
    e.ativar_cota_grupo(["A", "B", "C"], teto_tpm=1000)
    agora = time.time()
    e.marcar_cooldown_grupo("rate limit (RPM/TPM): quota", agora + 30, "retry")
    todos = all(e.cooldown_ate.get(n, 0) > agora + 25 for n in ("A", "B", "C"))
    caso("as 3 chaves ficaram em cooldown ate ~+30s", todos,
         f"cooldown_ate={e.cooldown_ate}")
    caso("bloqueio_tipo = retry para todas",
         all(e.bloqueio_tipo.get(n) == "retry" for n in ("A", "B", "C")))
    restante = e.menor_retry_restante()
    caso("menor_retry_restante ~ 30s (o pedido espera a vaga em vez de queimar a pool)",
         restante is not None and 25 <= restante <= 31, f"restante={restante}")
    # nao encurta bloqueio maior (quota do dia)
    e.marcar_cooldown_grupo("curto", agora + 5, "retry")
    caso("nao ENCURTA cooldown maior ja existente",
         e.cooldown_ate["A"] > agora + 25, f"ate={e.cooldown_ate['A']}")
    # roteador nao elege ninguem durante o cooldown do grupo
    cfg = cfg_base(compartilhada=True)
    eleg = roteador._elegiveis(e, cfg, cfg["chaves"], 2, time.time())
    caso("roteador: nenhuma chave elegivel durante cooldown de grupo",
         eleg == [], f"elegiveis={[c['nome'] for c in eleg]}")


def t7_modo_desligado():
    print("=" * 72)
    print("T7 - Modo desligado: comportamento por chave preservado")
    e = Estado()
    e.configurar_teto_reserva({"A": 1000, "B": 1000, "C": 1000})
    e.registrar_uso_local("A", 600)
    caso("tpm_disponivel(A) = 400 (so A)", e.tpm_disponivel("A") == 400)
    caso("tpm_disponivel(B) = 1000 (B independente)", e.tpm_disponivel("B") == 1000)
    caso("reservar(B, 800) ok (balde de B intocado)", e.reservar("B", 800) is True)
    caso("uso_60s_grupo desligado = (0, 0)", e.uso_60s_grupo() == (0, 0))
    v = Valvula()
    v.configurar("A", 2)
    v.configurar("B", 2)
    caso("valvula por chave: A esgota, B segue com fichas",
         v.consumir("A")[0] and v.consumir("A")[0] and not v.consumir("A")[0]
         and v.consumir("B")[0])


def main():
    print("TESTE REAL - cota compartilhada por projeto (F1/F2)")
    print("funcoes de producao, dados reais, zero mock, custo zero\n")
    t1_valvula_grupo()
    t2_estado_grupo_tpm()
    t3_reserva_grupo()
    t4_roteador_rpm_grupo()
    t5_roteador_tpm_grupo()
    t6_cooldown_grupo()
    t7_modo_desligado()
    print("=" * 72)
    total = len(FALHAS)
    if total:
        print(f"RESULTADO: {total} CASO(S) FALHARAM: {FALHAS}")
        return 1
    print("RESULTADO: TODOS OS CASOS PASSARAM")
    print("Custo: zero (sem rede, sem banco, sem cota).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
