"""Balde de fichas por chave — garante lambda <= mu por construcao.

Cada chave i recebe um balde com capacidade b_i = max(1, rpm_i) fichas e
reposicao continua a r_i = rpm_i/60 fichas por segundo. Um pedido so sai
para o Google consumindo 1 ficha; como um balde cheio corresponde exatamente
ao teto de uma janela rolante de 60s, o numero de disparos nunca excede o que
o Google aceitaria — ou seja, rho < 1 na porta de entrada, e o tempo de
espera por uma vaga e sempre FINITO e CALCULAVEL (proximo reabastecimento),
nunca a explosao 1/(1-rho) de uma fila saturada.

Quando o Google recusa (429/erro), a ficha e DEVOLVIDA: recusas nao entram
na conta dele, entao nao devem entrar na nossa. Sucesso gasta de verdade.
"""

import threading
import time


class Valvula:
    def __init__(self):
        self._lock = threading.Lock()
        self._baldes = {}

    def configurar(self, nome, rpm):
        rpm = float(rpm or 0)
        with self._lock:
            if rpm <= 0:
                self._baldes[nome] = {"ilimitado": True}
                return
            cap = max(1.0, rpm)
            taxa = cap / 60.0
            atual = self._baldes.get(nome)
            if atual and not atual.get("ilimitado"):
                atual["cap"] = cap
                atual["taxa"] = taxa
                atual["tokens"] = min(atual["tokens"], cap)
            else:
                self._baldes[nome] = {
                    "ilimitado": False,
                    "tokens": cap,
                    "cap": cap,
                    "taxa": taxa,
                    "quando": time.time(),
                }

    def _repor(self, balde, agora):
        delta = max(0.0, agora - balde["quando"])
        balde["tokens"] = min(balde["cap"], balde["tokens"] + delta * balde["taxa"])
        balde["quando"] = agora

    def consumir(self, nome):
        """Tenta gastar 1 ficha. Retorna (conseguiu, segundos_ate_haver_ficha)."""
        agora = time.time()
        with self._lock:
            balde = self._baldes.get(nome)
            if not balde or balde.get("ilimitado"):
                return True, 0.0
            self._repor(balde, agora)
            if balde["tokens"] >= 1.0:
                balde["tokens"] -= 1.0
                return True, 0.0
            falta = 1.0 - balde["tokens"]
            return False, (falta / balde["taxa"] if balde["taxa"] > 0 else 0.0)

    def devolver(self, nome):
        """Recusa do Google nao consome cota — devolve a ficha (limite: cap)."""
        with self._lock:
            balde = self._baldes.get(nome)
            if not balde or balde.get("ilimitado"):
                return
            balde["tokens"] = min(balde["cap"], balde["tokens"] + 1.0)

    def proxima_ficha(self, nome):
        with self._lock:
            balde = self._baldes.get(nome)
            if not balde or balde.get("ilimitado"):
                return 0.0
            self._repor(balde, time.time())
            falta = max(0.0, 1.0 - balde["tokens"])
            return falta / balde["taxa"] if balde["taxa"] > 0 else 0.0
