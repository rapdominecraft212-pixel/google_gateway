import asyncio
import sys
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import PASTA, config, db, gemini_api
from .agendador import Agendador
from .endpoints import medir_componentes, router
from .estado import Estado
from .onda import obter_scheduler

try:
    import diagnostico
except ImportError:  # execucao a partir de fora da raiz: garante o contrato no path
    sys.path.insert(0, str(PASTA))
    import diagnostico


def _sensor_heartbeat(app, inicio):
    """Thread de OS separada do event loop — e justamente por isso que ela
    sobrevive (e denuncia) um loop congelado. A cada 5s grava o heartbeat que
    o launcher le na hora da morte; a verdade dos 5s fica no arquivo, o JSONL
    so recebe 1x a cada 5min para nao virar spam."""
    tick_anterior = None
    ultima_emissao = 0.0
    while True:
        time.sleep(5)
        try:
            componentes, ticks = medir_componentes(app.state, tick_anterior)
            tick_anterior = ticks
            campos = dict(componentes)
            campos["uptime_s"] = int(time.time() - inicio)
            diagnostico.escrever_heartbeat(**campos)
            agora = time.time()
            if agora - ultima_emissao >= 300:
                ultima_emissao = agora
                diagnostico.emitir("heartbeat", "servidor", **campos)
        except Exception:
            continue  # o sensor nao morre por falha de diagnostico


def criar_app():
    cfg = config.carregar()
    cfg["chaves"] = config.chaves_validas(cfg)
    estado = Estado()
    db.inicializar()
    agendador = Agendador(estado, cfg)
    onda = obter_scheduler(estado, cfg)

    @asynccontextmanager
    async def _vida(app):
        gemini_api.aquecer()  # tira o 1o pedido do estado frio (DNS/TLS)
        agendador.iniciar()
        # Coracao do event loop: se este contador parar de andar enquanto o
        # socket continua aceitando conexao, o loop esta congelado (e nao o
        # processo). A thread sensor compara a cada 5s e grava no heartbeat.
        app.state._loop_ticks = 0

        async def _bater_loop():
            while True:
                await asyncio.sleep(1)
                app.state._loop_ticks += 1

        tarefa = asyncio.create_task(_bater_loop())
        threading.Thread(
            target=_sensor_heartbeat,
            args=(app, time.time()),
            name="sensor-heartbeat",
            daemon=True,
        ).start()
        try:
            yield
        finally:
            tarefa.cancel()
            agendador.parar()

    app = FastAPI(title="Monitor Gemini (Google)", version="2.0", lifespan=_vida)
    app.state.cfg = cfg
    app.state.estado = estado
    app.state.agendador = agendador
    app.state.onda = onda
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=PASTA / "site"), name="static")
    return app
