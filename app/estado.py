import threading
import time
from collections import deque
from datetime import datetime

from .janelas import JANELAS
from .valvula import Valvula

# Vida util de uma reserva de TPM: nenhum pedido valido vive mais que o timeout
# HTTP + folga; passado isso, o caminho que reservou falhou sem liberar.
_RESERVA_TTL_SEG = 150


def _timestamp(iso):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class Estado:
    def __init__(self):
        self._lock = threading.Lock()
        self.janelas = {}
        self.atualizado_em = {}
        self.em_voo = {}
        self.cooldown_ate = {}
        self.bloqueio_tipo = {}
        self.invalidas = set()
        self.erros = {}
        self.ultimo_erro_em = {}
        self.erros_recentes = {}
        self.uso_recente = {}
        self.em_voo = {}
        self.reservas = {}
        self.teto_reserva = {}
        self.episodios_503 = []  # [(ts, chave)] janela deslizante p/ corte de timeout
        # --- Racers adaptativos (budget de frota por media movel) ---
        # Sinal por pedido: chaves queimadas ate achar vencedor (503/hang + 1).
        # A deque guarda os ultimos sinais; racers_para_pedido() faz a media da
        # janela e DIVIDE pelos pedidos em voo — o budget total da frota fica
        # constante independente de quantos agentes batem na porta.
        self._racers_sinais = deque(maxlen=64)
        self.pedidos_em_voo = 0
        self.valvula = Valvula()
        # --- Cota compartilhada por projeto (cfg cota_compartilhada) ---
        # Quando ativo, TODAS as chaves do grupo compartilham UM balde de
        # RPM/TPM: e a realidade do Google (limites por PROJETO, nao por
        # chave - RELATORIO_GEMINI_API.md 8.1). Ver ativar_cota_grupo().
        self._cota_grupo = None
        # Fila global de rotacao de chaves (modelo "caixa": pega da FRENTE,
        # usada vai para o FUNDO). Compartilhada por TODOS os agentes/pedidos
        # do processo — e isso que garante que agentes paralelos sempre
        # recebem chaves "limpas" sem repeticao ate dar a volta completa.
        # deque: popleft/append sao O(1) reais; a eleicao roda a cada pedido.
        self._fila_chaves = deque()
        self.ultima_chave_usada = None
        try:
            from . import db
            self.ultima_chave_usada = db.ultima_chave_usada()
        except Exception:
            self.ultima_chave_usada = None

    def pegar_chaves(self, ordem_cfg, n, elegivel, rotacionar=True):
        """Entrega ate n nomes: SEMPRE a frente da fila; cada nome entregue
        vai para o fundo (rotacao justa compartilhada). Inelegiveis
        (cooldown/cota/cheia) ficam NA FRENTE esperando a vez — nao perdem a
        posicao. rotacionar=False e sonda (nao consome a vez).

        DUAS FASES, sem callback sob lock: `elegivel` consulta Estado e o
        Lock nao e reentrante — chama-lo sob self._lock seria deadlock
        garantido no primeiro pedido com TPM configurada. Fase 1 (sob lock):
        espreita ate n nomes da frente; fase 2 (sem lock): valida; fase 3
        (sob lock): consome os validos (fundo) e preserva os invalidos."""
        with self._lock:
            self._sincronizar_fila_locked(ordem_cfg)
            peek = list(self._fila_chaves)
        # fase 2 (SEM lock): valida varrendo a fila inteira — os primeiros n
        # elegiveis na ordem da frente para o fundo. Inelegiveis ficam onde
        # estao (na frente), esperando a vez, sem perder posicao.
        quota = max(1, int(n or 1))
        validos = []
        for nome in peek:
            if len(validos) >= quota:
                break
            if elegivel(nome):
                validos.append(nome)
        if not validos:
            return []
        with self._lock:
            if rotacionar:
                for nome in validos:
                    try:
                        self._fila_chaves.remove(nome)
                    except ValueError:
                        pass
                    self._fila_chaves.append(nome)
            return list(validos)

    def _sincronizar_fila_locked(self, ordem_cfg):
        """Mantem a fila igual a pool do config (UI adiciona/remove chaves a
        quente): preserva a ordem relativa dos existentes; novos entram no
        fundo. Construcao fresca (boot): comeca APÓS a ultima chave que o
        banco registra como usada — nunca volta para tras ao reiniciar."""
        alvo = list(dict.fromkeys(n for n in ordem_cfg if n))
        atuais = list(self._fila_chaves)
        if atuais == alvo:
            return
        em_alvo = set(alvo)
        preservados = [n for n in atuais if n in em_alvo]
        if preservados:
            novos = [n for n in alvo if n not in set(atuais)]
            self._fila_chaves = deque(preservados + novos)
            return
        fila = deque(alvo)
        ultima = self.ultima_chave_usada
        if ultima and ultima in fila:
            fila.rotate(-(list(fila).index(ultima) + 1))
        self._fila_chaves = fila

    def obter_ultima_chave(self):
        with self._lock:
            return self.ultima_chave_usada

    def avancar_cursor(self, nome):
        if not nome:
            return
        with self._lock:
            self.ultima_chave_usada = str(nome).strip()

    def rotacionar_fila(self, nome):
        """MODELO DA CAIXA: chave que acabou de ser DISPARADA vai para o fundo
        da fila global. O(1). Nome desconhecido (chave removida) e ignorado."""
        if not nome:
            return
        with self._lock:
            try:
                self._fila_chaves.remove(nome)
            except ValueError:
                pass
            self._fila_chaves.append(nome)
        self.avancar_cursor(nome)

    def resetar(self):
        with self._lock:
            self.__init__()

    def esquecer(self, chave):
        with self._lock:
            for tabela in (
                self.janelas,
                self.atualizado_em,
                self.em_voo,
                self.reservas,
                self.cooldown_ate,
                self.bloqueio_tipo,
                self.erros,
                self.ultimo_erro_em,
                self.erros_recentes,
                self.uso_recente,
            ):
                tabela.pop(chave, None)
            self.teto_reserva.pop(chave, None)
            if self.ultima_chave_usada == chave:
                self.ultima_chave_usada = None
            self.invalidas.discard(chave)
            try:
                self._fila_chaves.remove(chave)
            except ValueError:
                pass

    def marcar_uso(self, chave, janelas):
        with self._lock:
            self.janelas[chave] = janelas
            self.atualizado_em[chave] = time.time()
            self.invalidas.discard(chave)
            self.erros.pop(chave, None)
            if self.cooldown_ate.get(chave, 0) <= time.time():
                self.cooldown_ate.pop(chave, None)
                self.bloqueio_tipo.pop(chave, None)

    def marcar_erro(self, chave, mensagem, cooldown_seg=0):
        agora = time.time()
        with self._lock:
            self.erros[chave] = mensagem
            self.ultimo_erro_em[chave] = agora
            self.erros_recentes.setdefault(chave, []).append(agora)

    def marcar_retry(self, chave, mensagem, ate_ts):
        """Bloqueio curto e EXATO: a chave volta quando o Google mandou (RPM/TPM)."""
        agora = time.time()
        with self._lock:
            self.erros[chave] = mensagem
            self.ultimo_erro_em[chave] = agora
            self.erros_recentes.setdefault(chave, []).append(agora)
            self.cooldown_ate[chave] = max(agora + 0.5, ate_ts)
            self.bloqueio_tipo[chave] = "retry"

    def marcar_quota(self, chave, mensagem, ate_ts):
        """Bloqueio determinístico: cota esgotada até o reset (dia/mês)."""
        agora = time.time()
        with self._lock:
            self.erros[chave] = mensagem
            self.ultimo_erro_em[chave] = agora
            self.erros_recentes.setdefault(chave, []).append(agora)
            self.cooldown_ate[chave] = max(agora + 1, ate_ts)
            self.bloqueio_tipo[chave] = "quota"

    def marcar_sucesso(self, chave):
        with self._lock:
            self.erros.pop(chave, None)
            self.ultimo_erro_em.pop(chave, None)
            self.erros_recentes.pop(chave, None)
            self.cooldown_ate.pop(chave, None)
            self.bloqueio_tipo.pop(chave, None)
            self.ultima_chave_usada = str(chave).strip()

    def marcar_invalida(self, chave):
        with self._lock:
            self.invalidas.add(chave)
            self.erros[chave] = "key invalida"
            self.ultimo_erro_em[chave] = time.time()

    def registrar_503(self, chave):
        """Memoria de frota: 503 indica episodio de stress (nao falha isolada).

        Chama-se UMA vez por 503 recebido. A lista e podada pela janela maxima
        (600s) para nao crescer sem limite; episodio_ativo() poda pela janela
        configurada a cada leitura.
        """
        agora = time.time()
        with self._lock:
            self.episodios_503.append((agora, chave))
            self.episodios_503[:] = [p for p in self.episodios_503 if agora - p[0] <= 600]

    # --- Racers adaptativos: pedidos em voo (divide o budget da frota) ---
    def pedido_iniciar(self):
        with self._lock:
            self.pedidos_em_voo += 1

    def pedido_finalizar(self):
        with self._lock:
            self.pedidos_em_voo = max(0, self.pedidos_em_voo - 1)

    def registrar_sinal_racers(self, n_chaves_queimadas):
        """Guarda o sinal de UM pedido concluido: quantas chaves foram
        necessarias ate achar um vencedor (1 = acertou de primeira; N = queimou
        N-1 antes). A media movel da janela e o alvo bruto da frota."""
        with self._lock:
            self._racers_sinais.append(max(1, int(n_chaves_queimadas)))

    def racers_alvo(self, janela=10):
        """Media movel dos ultimos `janela` sinais: o budget bruto da frota."""
        with self._lock:
            sinais = list(self._racers_sinais)[-max(1, int(janela)):]
        return (sum(sinais) / len(sinais)) if sinais else 1.0

    def racers_para_pedido(self, cap_total, janela):
        """Quantas chaves este pedido deve disparar EM PARALELO agora.

        Alvo = media movel dos ultimos `janela` sinais (segue a tendencia de
        forma lenta: uma oscilacao isolada nao derruba o numero). Dividido
        pelos pedidos em voo para que N agentes concurrentes nao disparem
        N*cap de uma vez — o budget da frota e o cap_total, repartido.
        Clampado em [1, cap_total]. Fail-open: qualquer erro -> 1 (serial)."""
        try:
            alvo = self.racers_alvo(janela)
            with self._lock:
                em_voo = max(1, self.pedidos_em_voo)
            n = int(round(alvo / em_voo))
            if n < 1:
                n = 1
            if n > cap_total:
                n = cap_total
            return n
        except Exception:
            return 1

    def episodio_ativo(self, janela_seg=300, min_503=2, min_chaves=2):
        """True se ha sinal de episodio de stress: min_503 503s vindos de pelo
        menos min_chaves distintas dentro da janela. Exigir chaves distintas
        evita hedge por causa de UMA chave ruim sozinha (429 isolado de TPM
        nao e episodio de frota). Fail-open: qualquer erro -> False."""
        try:
            agora = time.time()
            with self._lock:
                recentes = [p for p in self.episodios_503 if agora - p[0] <= janela_seg]
                if len(recentes) < min_503:
                    return False
                distintas = {c for _, c in recentes}
                return len(distintas) >= min_chaves
        except Exception:
            return False

    def incrementar_em_voo(self, chave):
        with self._lock:
            self.em_voo[chave] = self.em_voo.get(chave, 0) + 1

    def configurar_teto_reserva(self, tpm_por_chave):
        """Teto de TPM por chave para a admissao preditiva (0/ausente = sem teto)."""
        with self._lock:
            self.teto_reserva = dict(tpm_por_chave or {})

    # ------------------------------------------------------------------
    # Cota compartilhada por projeto (cota_compartilhada: true no config)
    # ------------------------------------------------------------------
    def ativar_cota_grupo(self, nomes, teto_tpm=0):
        """Liga o balde de cota unico para o grupo de chaves (idempotente).

        Realidade do Google: as cotas sao por PROJETO, nao por chave. Quando
        todas as keys do config pertencem ao mesmo projeto, a frota inteira
        tem UM balde de RPM/TPM. Com o modo ativo:
          - tpm_disponivel()/teto_de()/balde_pristino()/reservar() passam a
            contar uso+reservas SOMADOS do grupo contra o teto de UM projeto
            (a admissao preditiva do roteador funciona sem nenhuma mudanca);
          - marcar_cooldown_grupo() trava a frota inteira num 429 de quota
            (queimar a pool so gastaria mais RPM do mesmo projeto morto).
        """
        nomes = [str(n) for n in (nomes or []) if n]
        with self._lock:
            if not nomes:
                self._cota_grupo = None
                return
            try:
                teto = int(teto_tpm or 0)
            except (TypeError, ValueError):
                teto = 0
            self._cota_grupo = {"nomes": set(nomes), "teto": teto}

    def desativar_cota_grupo(self):
        with self._lock:
            self._cota_grupo = None

    def cota_grupo_ativa(self):
        with self._lock:
            return self._cota_grupo is not None

    def _uso_minuto_grupo_lockado(self, agora):
        """Tokens usados pelo GRUPO no minuto-calendario corrente (soma)."""
        grupo = self._cota_grupo
        if not grupo:
            return 0
        balde = self._minuto_id(agora)
        total = 0
        for nome in grupo["nomes"]:
            lista = self.uso_recente.get(nome)
            if not lista:
                continue
            while lista and agora - lista[0][0] > 120:
                lista.pop(0)
            total += sum(par[1] for par in lista if self._minuto_id(par[0]) == balde)
        return total

    def _reserva_minuto_grupo_lockado(self, agora):
        """Reservas em voo do GRUPO no minuto-calendario corrente (soma)."""
        grupo = self._cota_grupo
        if not grupo:
            return 0
        balde = self._minuto_id(agora)
        total = 0
        for nome in grupo["nomes"]:
            lista = self.reservas.get(nome)
            if not lista:
                continue
            total += sum(valor for ts, valor in lista
                         if self._minuto_id(ts) == balde
                         and agora - ts <= _RESERVA_TTL_SEG)
        return total

    def uso_60s_grupo(self):
        """(reqs, tokens) somados de TODAS as chaves do grupo na janela 60s."""
        agora = time.time()
        with self._lock:
            grupo = self._cota_grupo
            if not grupo:
                return 0, 0
            reqs = 0
            tokens = 0
            for nome in grupo["nomes"]:
                lista = self.uso_recente.get(nome)
                if not lista:
                    continue
                while lista and agora - lista[0][0] > 60:
                    lista.pop(0)
                reqs += len(lista)
                tokens += sum(par[1] for par in lista)
            return reqs, tokens

    def tpm_disponivel_grupo(self):
        """Tokens livres do balde DO GRUPO (None = sem teto configurado)."""
        with self._lock:
            grupo = self._cota_grupo
            if not grupo:
                return None
            teto = grupo["teto"]
            if teto <= 0:
                return None
            agora = time.time()
            for nome in grupo["nomes"]:
                self._podar_reservas(nome, agora)
            return (teto
                    - self._uso_minuto_grupo_lockado(agora)
                    - self._reserva_minuto_grupo_lockado(agora))

    def balde_pristino_grupo(self):
        with self._lock:
            grupo = self._cota_grupo
            if not grupo:
                return True
            agora = time.time()
            for nome in grupo["nomes"]:
                self._podar_reservas(nome, agora)
            return (self._uso_minuto_grupo_lockado(agora) <= 0
                    and self._reserva_minuto_grupo_lockado(agora) <= 0)

    def marcar_cooldown_grupo(self, mensagem, ate_ts, tipo="retry"):
        """Bloqueia TODAS as chaves do grupo ate ate_ts (nunca encurta).

        Um 429 de quota sob cota compartilhada significa PROJETO esgotado:
        tentar outra chave so gasta mais RPM do mesmo balde morto. Marca a
        frota inteira de uma vez; o pedido espera a vaga (park/espera) ou
        falha rapido com Retry-After honesto, em vez de queimar 36 chaves.
        O bloqueio mais longo ja existente (ex.: quota do dia) e preservado.
        """
        agora = time.time()
        with self._lock:
            grupo = self._cota_grupo
            if not grupo:
                return
            for nome in grupo["nomes"]:
                self.erros[nome] = mensagem
                self.ultimo_erro_em[nome] = agora
                self.erros_recentes.setdefault(nome, []).append(agora)
                atual = self.cooldown_ate.get(nome, 0)
                if ate_ts >= atual:
                    self.cooldown_ate[nome] = max(atual, ate_ts, agora + 0.5)
                    self.bloqueio_tipo[nome] = tipo

    def reservar(self, chave, tokens):
        """Aparta tokens de um pedido ANTES de dispara-lo (admissao preditiva).

        Enquanto o pedido esta em voo o Google ainda nao contabilizou os tokens
        dele, mas eles entram na janela de TPM do minuto: sem reserva, um 2o
        pedido concorrente e admitido para a mesma chave e leva 429 garantido.
        Atomico: devolve False se a reserva estourar o teto (corrida entre
        threads) e o chamador deve descartar o candidato. Cada reserva registra
        a hora; expira sozinha (TTL) se um caminho de erro esquecer de chamar
        liberar() — vaza no maximo pelo prazo de UM pedido, nunca para sempre.
        """
        if tokens <= 0:
            return True
        agora = time.time()
        with self._lock:
            # --- modo cota compartilhada: balde do GRUPO (soma de todas) ---
            grupo = self._cota_grupo
            if grupo is not None:
                teto = grupo["teto"]
                if teto <= 0:
                    # sem teto configurado: comportamento ilimitado (igual ao
                    # caminho por chave sem teto)
                    self.reservas.setdefault(chave, []).append((agora, tokens))
                    return True
                for nome in grupo["nomes"]:
                    self._podar_reservas(nome, agora)
                atual = self._reserva_minuto_grupo_lockado(agora)
                uso = self._uso_minuto_grupo_lockado(agora)
                nova = atual + tokens
                if nova > teto:
                    # excecao do balde pristino do GRUPO: 1 oversized por
                    # balde vazio (mesma regra que o Google aplica ao projeto)
                    if not (atual <= 0 and uso <= 0):
                        return False
                self.reservas.setdefault(chave, []).append((agora, tokens))
                return True
            # --- caminho classico: balde por chave ---
            teto = self.teto_reserva.get(chave, 0)
            self._podar_reservas(chave, agora)
            # soma SOMENTE o minuto corrente: reserva do minuto anterior
            # pertence ao balde antigo e nao bloqueia o novo
            atual = self._reserva_minuto_atual(chave, agora)
            nova = atual + tokens
            if teto > 0 and nova > teto:
                # Excecao: balde pristino aceita UM oversized (Google aprova
                # 1 pedido >teto por balde vazio; o 2o levaria 429). Sem isso,
                # pedido maior que o teto nunca seria admitido em lugar nenhum.
                if not (atual <= 0 and self._uso_minuto_atual(chave, agora) <= 0):
                    return False
            self.reservas.setdefault(chave, []).append((agora, tokens))
            return True

    def liberar(self, chave, tokens):
        """Solta a reserva do pedido (sucesso OU falha: sempre chamar)."""
        if tokens <= 0:
            return
        with self._lock:
            self._podar_reservas(chave, time.time())
            lista = self.reservas.get(chave)
            if not lista:
                return
            falta = tokens
            while falta > 0 and lista:
                # remove as reservas mais antigas primeiro (FIFO) ate cobrir
                idx = min(range(len(lista)), key=lambda i: lista[i][0])
                quando, valor = lista.pop(idx)
                falta -= valor
            if falta < 0:
                lista.append((0.0, -falta))

    def _podar_reservas(self, chave, agora):
        lista = self.reservas.get(chave)
        if not lista:
            return
        validas = [r for r in lista if agora - r[0] <= _RESERVA_TTL_SEG]
        if validas:
            self.reservas[chave] = validas
        else:
            self.reservas.pop(chave, None)

    def _reserva_atual(self, chave):
        lista = self.reservas.get(chave)
        return sum(valor for _, valor in lista) if lista else 0

    def tokens_em_voo(self, chave):
        with self._lock:
            self._podar_reservas(chave, time.time())
            return self._reserva_atual(chave)

    def _minuto_id(self, ts):
        return int(float(ts) // 60)

    def _uso_minuto_atual(self, chave, agora):
        """Tokens concluidos no minuto-calendario corrente (modelo de baldes).

        O Google contabiliza TPM por minuto-calendario no recebimento, nao em
        janela rolante de 60s: apos a virada :00 o balde anterior zera. Contar
        a janela rolante aqui bloquearia chaves por ate 60s depois de liberadas
        (falso-negativo que estrangula a vazao pos-rajada).
        """
        balde = self._minuto_id(agora)
        lista = self.uso_recente.get(chave)
        if not lista:
            return 0
        while lista and agora - lista[0][0] > 120:
            lista.pop(0)
        return sum(par[1] for par in lista if self._minuto_id(par[0]) == balde)

    def _reserva_minuto_atual(self, chave, agora):
        """Reservas em voo do minuto corrente (pedidos recebidos neste balde).

        Reserva de minuto anterior pertence ao balde antigo e nao bloqueia o
        novo (o Google tambem nao a conta no balde novo).
        """
        balde = self._minuto_id(agora)
        lista = self.reservas.get(chave)
        if not lista:
            return 0
        return sum(valor for ts, valor in lista
                   if self._minuto_id(ts) == balde and agora - ts <= _RESERVA_TTL_SEG)

    def _uso_60s_lockado(self, chave):
        lista = self.uso_recente.get(chave)
        if not lista:
            return 0
        agora = time.time()
        while lista and agora - lista[0][0] > 60:
            lista.pop(0)
        return sum(par[1] for par in lista)

    def tpm_disponivel(self, chave):
        """Tokens livres para um NOVO pedido: teto - uso_minuto - reservas_minuto.

        Modelo de baldes de minuto-calendario (igual ao Google): apos :00 o
        minuto anterior zera e a folga volta ao teto. None = chave sem teto
        de TPM configurado (tratada como ilimitada).

        Com cota_compartilhada ativa, responde pelo GRUPO inteiro (o balde
        real e o do projeto, compartilhado por todas as chaves).
        """
        if self._cota_grupo is not None:
            return self.tpm_disponivel_grupo()
        with self._lock:
            teto = self.teto_reserva.get(chave, 0)
            if teto <= 0:
                return None
            agora = time.time()
            self._podar_reservas(chave, agora)
            return teto - self._uso_minuto_atual(chave, agora) - self._reserva_minuto_atual(chave, agora)

    def teto_de(self, chave):
        """Teto TPM configurado da chave (0 = sem teto).

        Com cota_compartilhada ativa, responde o teto do GRUPO (projeto).
        """
        with self._lock:
            if self._cota_grupo is not None:
                try:
                    return int(self._cota_grupo["teto"] or 0)
                except (TypeError, ValueError):
                    return 0
            try:
                return int(self.teto_reserva.get(chave, 0) or 0)
            except (TypeError, ValueError):
                return 0

    def balde_pristino(self, chave):
        """True se o balde do minuto corrente esta vazio (nada usado nem
        reservado). O Google aceita UM pedido oversized (>teto) por balde
        pristino (prova: tin=326k com 200 no historico); o 2o no mesmo
        minuto leva 429. Sem isso, pedido maior que o teto seria recusado
        por TODAS as chaves — deadlock de admissao.

        Com cota_compartilhada ativa, responde pelo GRUPO inteiro.
        """
        if self._cota_grupo is not None:
            return self.balde_pristino_grupo()
        with self._lock:
            agora = time.time()
            self._podar_reservas(chave, agora)
            return (self._uso_minuto_atual(chave, agora) <= 0
                    and self._reserva_minuto_atual(chave, agora) <= 0)

    def registrar_uso_local(self, chave, tokens=0):
        agora = time.time()
        with self._lock:
            lista = self.uso_recente.setdefault(chave, [])
            lista.append((agora, tokens))
            if len(lista) > 4000:
                lista[:] = [par for par in lista if agora - par[0] <= 60]

    def uso_60s(self, chave):
        agora = time.time()
        with self._lock:
            lista = self.uso_recente.get(chave)
            if not lista:
                return 0, 0
            while lista and agora - lista[0][0] > 60:
                lista.pop(0)
            return len(lista), sum(par[1] for par in lista)

    def quando_libera_rpm(self):
        """Segundos ate a entrada mais antiga de qualquer chave sair da janela
        de 60s (vaga exata de RPM; None se ninguem consumiu nada ainda)."""
        agora = time.time()
        melhor = None
        with self._lock:
            for lista in self.uso_recente.values():
                while lista and agora - lista[0][0] > 60:
                    lista.pop(0)
                if not lista:
                    continue
                restante = 60.0 - (agora - lista[0][0])
                if melhor is None or restante < melhor:
                    melhor = restante
        return melhor

    def decrementar_em_voo(self, chave):
        with self._lock:
            self.em_voo[chave] = max(0, self.em_voo.get(chave, 0) - 1)

    def percent_mais_cheio(self, chave):
        with self._lock:
            jan = self.janelas.get(chave) or {}
        pcts = []
        for nome in JANELAS:
            janela = jan.get(nome) or {}
            valor = janela.get("percent")
            if valor is not None:
                pcts.append(float(valor))
        return max(pcts) if pcts else None

    def percent_ativo(self, chave):
        agora = time.time()
        with self._lock:
            jan = self.janelas.get(chave) or {}
        pcts = []
        for nome in JANELAS:
            janela = jan.get(nome) or {}
            reset_ts = _timestamp(janela.get("resetsAt"))
            if reset_ts is not None and reset_ts <= agora:
                continue
            valor = janela.get("percent")
            if valor is not None:
                pcts.append(float(valor))
        return max(pcts) if pcts else None

    def menor_cooldown_restante(self):
        agora = time.time()
        with self._lock:
            futuros = [
                ate - agora
                for nome, ate in self.cooldown_ate.items()
                if ate > agora and nome not in self.invalidas
            ]
        return min(futuros) if futuros else None

    def menor_retry_restante(self):
        agora = time.time()
        with self._lock:
            futuros = [
                ate - agora
                for nome, ate in self.cooldown_ate.items()
                if ate > agora
                and nome not in self.invalidas
                and self.bloqueio_tipo.get(nome) == "retry"
            ]
        return min(futuros) if futuros else None

    def menor_quota_reset(self):
        agora = time.time()
        with self._lock:
            futuros = [
                ate - agora
                for nome, ate in self.cooldown_ate.items()
                if ate > agora
                and nome not in self.invalidas
                and self.bloqueio_tipo.get(nome) == "quota"
            ]
        return min(futuros) if futuros else None

    def quando_libera(self, chaves, limite_esgotada):
        agora = time.time()
        eventos = []
        with self._lock:
            for chave in chaves:
                nome = chave["nome"]
                if chave.get("ativa") is False or nome in self.invalidas:
                    continue
                ate = self.cooldown_ate.get(nome, 0)
                if ate > agora:
                    eventos.append(ate)
                    continue
                jan = self.janelas.get(nome) or {}
                for janela in jan.values():
                    percent = janela.get("percent")
                    reset_ts = _timestamp(janela.get("resetsAt"))
                    if (
                        percent is not None
                        and float(percent) >= limite_esgotada
                        and reset_ts is not None
                        and reset_ts > agora
                    ):
                        eventos.append(reset_ts)
                        break
        if not eventos:
            return None
        return max(0, min(eventos) - agora)

    def erros_10min(self, chave):
        agora = time.time()
        with self._lock:
            return [t for t in self.erros_recentes.get(chave, []) if agora - t < 600]

    def snapshot_chave(self, chave):
        agora = time.time()
        with self._lock:
            cooldown_restante = max(0, int(self.cooldown_ate.get(chave, 0) - agora))
            ate = self.cooldown_ate.get(chave)
            bloqueado_ate = (
                datetime.fromtimestamp(ate).isoformat(timespec="seconds")
                if ate and ate > agora
                else None
            )
            return {
                "janelas": self.janelas.get(chave),
                "atualizado_em": self.atualizado_em.get(chave),
                "em_voo": self.em_voo.get(chave, 0),
                "cooldown_restante": cooldown_restante,
                "bloqueio_tipo": self.bloqueio_tipo.get(chave),
                "bloqueado_ate": bloqueado_ate,
                "invalida": chave in self.invalidas,
                "erro": self.erros.get(chave),
                "ultimo_erro_em": self.ultimo_erro_em.get(chave),
            }
