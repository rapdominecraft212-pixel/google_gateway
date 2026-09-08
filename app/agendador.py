import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import PASTA, db, gemini_api, janelas, modelos
from .config import chaves_ativas

try:
    import diagnostico
except ImportError:  # execucao a partir de fora da raiz: garante o contrato no path
    import sys

    sys.path.insert(0, str(PASTA))
    import diagnostico


def _emitir_excecao_poller():
    """Registra a excecao real no log de diagnostico em vez de engoli-la em
    silencio; o loop continua vivo (comportamento identico ao anterior, so
    que agora a causa fica nomeada)."""
    try:
        diagnostico.emitir("excecao_poller", "servidor", traceback=traceback.format_exc(limit=20))
    except Exception:
        pass


class Agendador:
    def __init__(self, estado, cfg):
        self.estado = estado
        self.cfg = cfg
        self._parar = threading.Event()
        self._thread = None

    def iniciar(self):
        if self._thread and self._thread.is_alive():
            return
        self._parar.clear()
        self._thread = threading.Thread(target=self._loop, name="poll-uso", daemon=True)
        self._thread.start()

    def vivo(self):
        """True se a thread do poller esta de pe (sensor de heartbeat/saude)."""
        return bool(self._thread and self._thread.is_alive())

    def parar(self):
        self._parar.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=15)

    def _loop(self):
        while not self._parar.is_set():
            try:
                gemini_api.aquecer()  # mantem DNS/TLS mornos entre ciclos
                self.atualizar_tudo()
            except Exception:
                _emitir_excecao_poller()
            self._parar.wait(int(self.cfg.get("intervalo_uso_seg", 60)))

    def atualizar_tudo(self):
        try:
            modelos.sincronizar(self.cfg)
        except Exception:
            _emitir_excecao_poller()
        chaves = chaves_ativas(self.cfg)
        resultados = {}
        with ThreadPoolExecutor(max_workers=16) as pool:
            futuros = {pool.submit(self.atualizar_chave, chave): chave["nome"] for chave in chaves}
            for futuro in as_completed(futuros):
                nome = futuros[futuro]
                try:
                    resultados[nome] = futuro.result()
                except Exception as erro:
                    resultados[nome] = {"ok": False, "motivo": str(erro)}
        return resultados

    def atualizar_chave(self, chave):
        nome = chave["nome"]
        if chave["tipo"] != "gemini":
            return {"ok": True, "sem_api_de_uso": True}
        timeout = max(15, int(self.cfg.get("timeout_http_seg", 120)) // 2)
        ok, resp = gemini_api.validar_chave(chave["key"], timeout=timeout)
        if not ok:
            if "401" in str(resp) or "403" in str(resp):
                self.estado.marcar_invalida(nome)
            else:
                self.estado.marcar_erro(nome, resp)
            return {"ok": False, "motivo": resp}
        limites = janelas.limites_por_chave(self.cfg, chave)
        uso = db.uso_bruto(nome)
        jan = janelas.calcular(uso, limites)
        self.estado.marcar_uso(nome, jan)
        db.registrar_snapshot(nome, jan, {"uso": uso, "limites": limites})
        return {"ok": True}
