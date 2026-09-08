import argparse
import atexit
import logging
import os
import signal
import socket
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.config import carregar
from app.main import criar_app

try:
    import diagnostico
except ImportError:
    diagnostico = None  # servidor nao pode deixar de subir por falta de sensor

PASTA = Path(__file__).resolve().parent

ADDR_IN_USE = frozenset({98, 10048, 48})  # EADDRINUSE (Linux/macOS/Windows)


def _emitir(evento, **detalhe):
    """Observacional puro: diagnostico nunca derruba o boot do servidor."""
    try:
        if diagnostico is not None:
            diagnostico.emitir(evento, "servidor", **detalhe)
    except Exception:
        pass


def configurar_log():
    arquivo = PASTA / "servidor.log"
    formato = logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s")
    raiz = logging.getLogger()
    raiz.setLevel(logging.INFO)
    raiz.handlers.clear()
    para_arquivo = RotatingFileHandler(arquivo, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    para_arquivo.setFormatter(formato)
    raiz.addHandler(para_arquivo)
    if sys.stderr is not None:
        para_console = logging.StreamHandler(sys.stderr)
        para_console.setFormatter(formato)
        raiz.addHandler(para_console)
    return logging.getLogger("servidor")


def porta_livre(host, porta):
    """Retorna True se a porta puder ser vinculada agora (teste rápido)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host if host != "0.0.0.0" else "127.0.0.1", porta))
        return True
    except OSError as erro:
        if erro.errno in ADDR_IN_USE:
            return False
        raise
    finally:
        s.close()


def adquirir_trava(porta):
    """Trava de instância única por porta (atômica via O_EXCL).

    Garante que apenas UM processo por porta chegue a tentar o bind. Se já
    houver um gateway vivo na porta, recusa iniciar (evita o cenário em que
    um segundo processo falha o bind e o serviço 'some' numa porta errada).
    Se a trava estiver obsoleta (PID morto), assume o controle.
    """
    trava = PASTA / f".gateway_lock_{porta}"
    if trava.exists():
        try:
            pid = int(trava.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            pid = None
        if pid and _processo_vivo(pid):
            # Sensorio: registra o conflito ANTES de recusar iniciar, para a
            # morte com codigo 1 ter causa nomeada no log do launcher.
            _emitir("lockfile_conflito", esperado=os.getpid(), encontrado=pid, vivo=True)
            raise SystemExit(
                f"ERRO: já existe um gateway vivo (PID {pid}) na porta {porta}. "
                f"Encerre esse processo antes de iniciar outro."
            )
        trava.unlink(missing_ok=True)
    fd = os.open(trava, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, str(os.getpid()).encode("utf-8"))
    finally:
        os.close(fd)
    # Self-check pos-aquisicao: se o conteudo nao e o nosso PID e o outro esta
    # vivo, duas instancias se pisaram (corrida rara). Observacional apenas.
    try:
        conteudo = int(trava.read_text(encoding="utf-8").strip())
        if conteudo != os.getpid() and _processo_vivo(conteudo):
            _emitir("lockfile_conflito", esperado=os.getpid(), encontrado=conteudo, vivo=True)
    except (ValueError, OSError):
        pass
    return trava


def _processo_vivo(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def principal():
    log = configurar_log()
    try:
        import uvicorn
    except ImportError:
        print("Dependencia faltando. Rode: pip install -r requirements.txt")
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Gateway Monitor Google")
    parser.add_argument("--host", help="host de bind (default: config 'host' ou 0.0.0.0)")
    parser.add_argument("--port", type=int, help="porta (default: config 'porta' ou 8011)")
    parser.add_argument("--config", help="caminho do config.json alternativo")
    args = parser.parse_args()

    if args.config:
        os.environ["MONITOR_CONFIG"] = str(Path(args.config).resolve())

    cfg = carregar()
    host = args.host or cfg.get("host", "0.0.0.0")
    porta = args.port or int(cfg.get("porta", 8011))

    trava = adquirir_trava(porta)
    atexit.register(_liberar_trava, trava)

    if not porta_livre(host, porta):
        _liberar_trava(trava)
        raise SystemExit(
            f"ERRO: a porta {porta} já está em uso (outro processo a segura). "
            f"O gateway NÃO sobe numa porta diferente para não 'sumir' sem aviso. "
            f"Libere a porta ou encerre o processo que a ocupa."
        )

    log.info("Iniciando gateway na porta %s (host=%s)", porta, host)
    # Marcador de boot do lado do servidor (fonte="servidor"). "servidor_pronto"
    # e um novo nome de evento no campo `evento` (vocabulario aberto do
    # contrato em diagnostico.py); o schema top-level nao muda.
    _emitir("servidor_pronto", pid=os.getpid(), porta=porta, host=host)
    uvicorn.run(criar_app(), host=host, port=porta, log_level="info")


def _liberar_trava(trava):
    try:
        trava.unlink(missing_ok=True)
    except OSError:
        pass


if __name__ == "__main__":
    principal()
