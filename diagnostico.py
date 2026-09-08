"""Contrato de diagnostico compartilhado entre launcher e servidor.

Objetivo: transformar o RESULTADO ("morreu codigo -1") em PROCESSO (causa
nomeada, na ordem certa, discriminando alternativas). Este modulo e a UNICA
peca que os dois lados concordam; nao contem logica de coleta especifica
(essa vive em quem a usa). Regra do projeto: nada de mock — tudo aqui escreve
e le arquivos/estado reais.

Esquema do event log (JSONL, uma linha por evento, logs/eventos-diagnostico.log):
    {
      "ts":    "2026-09-06T15:09:28.123",  # ISO local com milissegundos
      "fonte": "launcher" | "servidor" | "windows",
      "evento": <vocabulario abaixo>,
      "pid":   11600,                       # processo sobre o qual o evento fala
      "ppid":  11980,                       # pai, se conhecido (senao null)
      "detalhe": { ...campos especificos... }
    }

Vocabulario de eventos (a causa tem que aparecer como um destes ANTES do
"filho_morto", para que a ordem causa->efeito seja verificavel):
    launcher_start      {motivo: boot|login|manual|reinicio}
    launcher_exit       {motivo}
    filho_spawn         {cmd, porta}
    power_event         {tipo: suspend|resume|shutdown|logoff|console_disconnect|...}
    session_event       {tipo: logon|logoff|lock|unlock|...}
    watchdog_kill       {falhas_saude, ultima_resposta_ms}
    filho_morto         {codigo, codigo_nome, codigo_classe, duracao_vivo_s,
                         ultimo_heartbeat_ts, causa_provavel, eventos_windows:[...]}
    heartbeat           (escrito via escrever_heartbeat, arquivo proprio)
    db_readonly_causa   {erro, caminho, atributos, lockers}
    excecao_poller      {traceback}
    saude_real          {ok, componentes}
    lockfile_conflito   {esperado, encontrado, vivo}
"""
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

PASTA = Path(__file__).resolve().parent
LOGS = PASTA / "logs"
LOG_EVENTOS = LOGS / "eventos-diagnostico.log"
ARQUIVO_HEARTBEAT = LOGS / "heartbeat-servidor.json"

_lock = threading.Lock()


def _ts_local():
    return datetime.now().isoformat(timespec="milliseconds")


def emitir(evento, fonte, pid=None, ppid=None, **detalhe):
    """Anexa um evento JSONL ao log de diagnostico. Nunca lanca excecao:
    diagnostico nao pode derrubar o que esta sendo diagnosticado."""
    registro = {
        "ts": _ts_local(),
        "fonte": fonte,
        "evento": evento,
        "pid": pid if pid is not None else os.getpid(),
        "ppid": ppid,
        "detalhe": detalhe or {},
    }
    linha = (json.dumps(registro, ensure_ascii=False, separators=(",", ":")) + "\n")
    dados = linha.encode("utf-8")
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        with _lock:
            # O_APPEND + escrita unica de linha curta (< PIPE_BUF) minimiza
            # entrelacamento entre os dois processos. Escrita e rara.
            fd = os.open(str(LOG_EVENTOS), os.O_WRONLY | os.O_CREAT | os.O_APPEND)
            try:
                os.write(fd, dados)
            finally:
                os.close(fd)
    except OSError:
        pass
    return registro


def escrever_heartbeat(**campos):
    """Grava o ultimo heartbeat do servidor (arquivo unico, escrita atomica).
    O launcher le isto ao detectar a morte para responder 'o que ele fazia
    no ultimo instante saudavel'."""
    payload = {"ts": _ts_local(), "pid": os.getpid(), **campos}
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        tmp = str(ARQUIVO_HEARTBEAT) + f".tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, str(ARQUIVO_HEARTBEAT))
    except OSError:
        pass
    return payload


def ler_heartbeat():
    """Le o ultimo heartbeat (ou None). Usado pelo launcher na hora da morte."""
    try:
        with open(ARQUIVO_HEARTBEAT, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


# Codigos de saida conhecidos do Windows (DWORD sem sinal). O -1
# (0xFFFFFFFF) que vinha aparecendo e generico: 'processo terminado
# externamente'. Os 0xC0000xxx sao crashes nativos com causa especifica.
_CODIGOS_WIN = {
    0x00000000: ("EXIT_OK", "normal"),
    0x00000001: ("EXIT_GENERIC", "python SystemExit/erro"),
    0xC000013A: ("STATUS_CONTROL_C_EXIT", "console fechada / Ctrl+C"),
    0xFFFFFFFF: ("STATUS_GENERIC_TERMINATED", "terminado externamente (kill/sessao/energia)"),
    0xC0000005: ("STATUS_ACCESS_VIOLATION", "crash nativo: acesso a memoria invalida"),
    0xC00000FD: ("STATUS_STACK_OVERFLOW", "crash nativo: estouro de pilha"),
    0xC0000374: ("STATUS_HEAP_CORRUPTION", "crash nativo: corrupcao de heap"),
    0xC0000409: ("STATUS_STACK_BUFFER_OVERRUN", "crash nativo: fail-fast/abort"),
    0xC000027A: ("STATUS_FATAL_APP_EXIT", "abort() nativo"),
    0x40010004: ("DBG_TERMINATE_PROCESS", "depurador/externo terminou o processo"),
    0x80070005: ("E_ACCESSDENIED", "negado por permissao"),
}


def decodificar_codigo(codigo):
    """Converte o exit code (tal como subprocess devolve no Windows) em
    (nome, classe). Aceita assinado ou sem sinal mascarando para 32 bits."""
    if codigo is None:
        return ("DESCONHECIDO", "sem codigo")
    u = codigo & 0xFFFFFFFF
    if u in _CODIGOS_WIN:
        return _CODIGOS_WIN[u]
    if u >= 0xC0000000:
        return (f"STATUS_0x{u:08X}", "crash nativo (codigo Windows nao catalogado)")
    return (f"EXIT_{u}", "desconhecido")


def agora_monotnico():
    return time.monotonic()
