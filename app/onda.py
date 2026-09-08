"""WaveScheduler: pacing de pedidos grandes por minuto-calendario, POR CHAVE.

Um pedido e "grande" quando estimativa >= onda_limite_tokens. Cada chave pode
disparar no maximo 1 grande por minuto-calendario (lease por chave); N grandes
concorrentes em N chaves distintas passam todos na hora (capacidade da frota =
n. de chaves/minuto). Quando TODAS as chaves uteis estao com o minuto ocupado,
o pedido estaciona ate a proxima fronteira :00 (+adianto+jitter) ou e
rejeitado com honestidade ("onda:...", retry_after>=1) quando a fila esta
cheia, a espera excede onda_max_espera_seg ou o deadline nao comporta.
Pedido pequeno (abaixo do limite) nunca entra no pacing: passa direto.

Config: chaves flat top-level (onda_limite_tokens, onda_max_na_fila,
onda_max_espera_seg, onda_jitter_max_ms, onda_adianto_ms). Leitura aceita
tambem o dicionario aninhado cfg["onda"] com nomes com ou sem prefixo "onda_"
(ex.: {"onda": {"limite_tokens": 5}}), de forma defensiva.

Relogio: SEMPRE time.time() real nos metodos do scheduler. As unicas funcoes
que aceitam timestamp sao as puras bucket_id/next_wave_ts (para testes
unitarios rapidos); o caminho de producao (reivindicar/minuto_ocupado/
depth) nunca recebe relogio injetado.

Locks: o scheduler tem lock proprio (self._lock) e NUNCA toca em
Estado._lock nem chama metodos de Estado sob lock (zero aninhamento).
Leases ficam em dict interno (_disparos: nome -> minuto_id) e a instancia e
compartilhada por objeto Estado via obter_scheduler() (atributo dinamico
estado._onda), de modo que sobrevivem entre requests sem alterar estado.py.
"""

import random
import threading
import time

DEFAULT_ONDA_LIMITE_TOKENS = 200000
DEFAULT_ONDA_MAX_NA_FILA = 48
DEFAULT_ONDA_MAX_ESPERA_SEG = 30
DEFAULT_ONDA_JITTER_MAX_MS = 2000
DEFAULT_ONDA_ADIANTO_MS = 250


def _ler_cfg(cfg, nome_completo, default):
    """Leitura defensiva: top-level flat OU aninhado cfg["onda"]."""
    if not isinstance(cfg, dict):
        return default
    if nome_completo in cfg and cfg[nome_completo] is not None:
        return cfg[nome_completo]
    sub = cfg.get("onda")
    if isinstance(sub, dict):
        if nome_completo in sub and sub[nome_completo] is not None:
            return sub[nome_completo]
        curto = nome_completo
        if curto.startswith("onda_"):
            curto = curto[len("onda_"):]
        if curto in sub and sub[curto] is not None:
            return sub[curto]
    return default


def _como_int(valor, default):
    try:
        if isinstance(valor, bool):
            return int(valor)
        return int(float(valor))
    except (TypeError, ValueError):
        return default


def bucket_id(ts=None):
    """Minuto-calendario de um timestamp: int(ts // 60)."""
    if ts is None:
        ts = time.time()
    return int(float(ts) // 60)


def next_wave_ts(agora=None, adianto_ms=0):
    """Timestamp da proxima fronteira :00 (+adianto_ms/1000 de stagger).

    O adianto e SOMADO apos a fronteira (apesar do nome sugerir
    antecipacao): garante que o disparo caia no minuto NOVO (minuto_id novo)
    e evita thundering herd exatamente no tick :00. Com adianto=0 retorna a
    fronteira exata.
    """
    if agora is None:
        agora = time.time()
    try:
        ad = float(adianto_ms or 0) / 1000.0
    except (TypeError, ValueError):
        ad = 0.0
    base = (int(float(agora) // 60) + 1) * 60
    return base + ad


def jitter_seg(jitter_max_ms=2000):
    """Jitter uniforme em segundos: [0, jitter_max_ms/1000]."""
    try:
        mx = float(jitter_max_ms or 0) / 1000.0
    except (TypeError, ValueError):
        return 0.0
    if mx <= 0:
        return 0.0
    return random.uniform(0.0, mx)


_cache_lock = threading.Lock()


def obter_scheduler(estado, cfg):
    """Retorna o WaveScheduler compartilhado daquele objeto Estado.

    A instancia fica em estado._onda (atributo dinamico; sem alterar
    estado.py) para que leases sobrevivam entre requests. Atualiza .cfg a
    cada chamada para enxergar mutacoes do dict de config. Sem aninhamento
    de locks (apenas getattr/setattr, sem Estado._lock).
    """
    if estado is not None:
        try:
            existente = getattr(estado, "_onda", None)
            if isinstance(existente, WaveScheduler):
                try:
                    existente.cfg = cfg
                except Exception:
                    pass
                return existente
        except Exception:
            pass
    novo = WaveScheduler(estado, cfg)
    if estado is not None:
        try:
            with _cache_lock:
                existente2 = getattr(estado, "_onda", None)
                if isinstance(existente2, WaveScheduler):
                    try:
                        existente2.cfg = cfg
                    except Exception:
                        pass
                    return existente2
                setattr(estado, "_onda", novo)
        except Exception:
            pass
    return novo


class WaveScheduler:
    def __init__(self, estado, cfg):
        self.estado = estado
        self.cfg = cfg
        self._lock = threading.Lock()
        self._disparos = {}
        self._estacionados = 0

    def deve_agendar(self, estimativa):
        """True quando o pedido e grande o bastante para o pacing
        (estimativa >= limite). Sem flag on/off: pedido pequeno sempre passa
        direto, pedido grande sempre entra no pacing por chave."""
        try:
            est = int(estimativa or 0)
        except (TypeError, ValueError):
            return False
        return est >= self._limite_tokens()

    def _limite_tokens(self):
        return _como_int(_ler_cfg(self.cfg, "onda_limite_tokens", DEFAULT_ONDA_LIMITE_TOKENS),
                         DEFAULT_ONDA_LIMITE_TOKENS)

    def _max_na_fila(self):
        return _como_int(_ler_cfg(self.cfg, "onda_max_na_fila", DEFAULT_ONDA_MAX_NA_FILA),
                         DEFAULT_ONDA_MAX_NA_FILA)

    def _max_espera_seg(self):
        v = _ler_cfg(self.cfg, "onda_max_espera_seg", DEFAULT_ONDA_MAX_ESPERA_SEG)
        try:
            f = float(v)
        except (TypeError, ValueError):
            return float(DEFAULT_ONDA_MAX_ESPERA_SEG)
        return f

    def _jitter_max_ms(self):
        return _como_int(_ler_cfg(self.cfg, "onda_jitter_max_ms", DEFAULT_ONDA_JITTER_MAX_MS),
                         DEFAULT_ONDA_JITTER_MAX_MS)

    def _adianto_ms(self):
        return _como_int(_ler_cfg(self.cfg, "onda_adianto_ms", DEFAULT_ONDA_ADIANTO_MS),
                         DEFAULT_ONDA_ADIANTO_MS)

    def max_espera_seg(self):
        """Orcamento total de estacionamento por pedido (publico p/ proxy)."""
        return self._max_espera_seg()

    def max_na_fila(self):
        """Teto de pedidos estacionados simultaneos (publico p/ proxy)."""
        return self._max_na_fila()

    def reivindicar(self, nome):
        """Marca (nome -> minuto atual) atomicamente.

        True = lease obtido (pode disparar); False = outra thread/pedido ja
        ocupou esta chave neste minuto (corrida perdida: re-eleger outra).
        """
        if not nome:
            return True
        balde = bucket_id(time.time())
        with self._lock:
            self._podar(balde)
            if self._disparos.get(nome) == balde:
                return False
            self._disparos[nome] = balde
            return True

    def liberar_minuto(self, nome):
        """Libera lease obtido sem dispatch subsequente (evita prender a
        chave ate o fim do minuto quando o fire foi abortado)."""
        if not nome:
            return
        with self._lock:
            self._disparos.pop(nome, None)

    def tempo_para_proxima_onda(self):
        """Segundos ate :00 + adianto + jitter (quanto estacionar)."""
        agora = time.time()
        try:
            base = next_wave_ts(agora, self._adianto_ms())
            return max(0.0, base - agora + jitter_seg(self._jitter_max_ms()))
        except Exception:
            try:
                return max(0.0, next_wave_ts(agora, 0) - agora)
            except Exception:
                return 0.0

    def entrar_fila(self):
        """True se ha vaga de estacionamento (incrementa); False se cheia."""
        try:
            cap = self._max_na_fila()
        except Exception:
            cap = DEFAULT_ONDA_MAX_NA_FILA
        with self._lock:
            if self._estacionados >= cap:
                return False
            self._estacionados += 1
            return True

    def sair_fila(self):
        """Libera vaga de estacionamento (idempotente, nunca negativo)."""
        with self._lock:
            if self._estacionados > 0:
                self._estacionados -= 1

    def _podar(self, balde):
        for k in [k for k, v in self._disparos.items() if balde - v > 2]:
            self._disparos.pop(k, None)

    def minuto_ocupado(self, nome):
        """True se essa chave ja disparou um grande neste minuto-calendario."""
        if not nome:
            return False
        balde = bucket_id(time.time())
        with self._lock:
            self._podar(balde)
            return self._disparos.get(nome) == balde

    def depth(self):
        """N. de chaves com disparo grande neste minuto (observabilidade)."""
        balde = bucket_id(time.time())
        with self._lock:
            self._podar(balde)
            return sum(1 for v in self._disparos.values() if v == balde)
