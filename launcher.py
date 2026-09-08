import atexit
import ctypes
import os
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from ctypes import wintypes
from datetime import datetime, timedelta
from pathlib import Path

import diagnostico

BASE = Path(r"C:\Users\User\Desktop\Monitor-Google")
LOGS = BASE / "logs"
BACKOFF_STEPS = [2, 5, 10, 30]


def ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    linha = f"[{ts()}] {msg}"
    if sys.stdout is not None:
        try:
            print(linha, flush=True)
        except Exception:
            pass
    try:
        with open(LOGS / "launcher.log", "a", encoding="utf-8") as f:
            f.write(linha + "\n")
    except Exception:
        pass


def _pid_vivo(pid):
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    except Exception:
        return True


def _ja_rodando():
    caminho = BASE / "launcher.pid"
    if not caminho.exists():
        return False
    try:
        pid = int(caminho.read_text().strip())
    except (ValueError, OSError):
        return False
    return pid != os.getpid() and _pid_vivo(pid)


def _porta_ja_atendida(saude_url):
    # Se algo ja responde nessa porta (outro launcher ou instancia manual),
    # nao vamos iniciar uma segunda copia: ela so morreria com conflito de porta.
    try:
        with urllib.request.urlopen(saude_url, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


# ===========================================================================
# SENSORES DE DIAGNOSTICO (contrato em diagnostico.py). Regra inegociavel:
# nenhum sensor pode derrubar ou travar o launcher -- tudo try/except e
# melhor-esforco. Eles apenas ACRESCENTAM informacao; o comportamento de
# supervisao (spawn/backoff/watchdog) segue identico ao de antes.
# ===========================================================================

def _motivo_start():
    """Heuristica do motivo pelo qual este launcher comecou (sensor 1).
    Ordem (da mais certa para a mais fraca):
      --boot na linha de comando -> 'boot' (tarefa MonitorGemini-Boot, ver
          instalar_boot.ps1; e a unica via que dispara sem login interativo).
      launcher.pid existente apontando para PID MORTO -> 'reinicio': o
          launcher anterior nao conseguiu limpar o pidfile (foi morto ou a
          maquina caiu), entao esta subida e uma re-subida pos-mortem.
      sessao interativa -> 'login': USERNAME definido E SESSIONNAME presente
          e diferente de 'Services'/'DISKDRIVE' (servico/SYSTEM roda na
          sessao 0 sem desktop; o .lnk de Startup roda em sessao Console/RDP
          com SESSIONNAME='Console' ou 'RDP-Tcp#N').
      caso contrario -> 'manual' (ex.: execuacao ad hoc sem ambiente de
          console, ssh, etc.).
    """
    if "--boot" in sys.argv:
        return "boot"
    try:
        caminho = BASE / "launcher.pid"
        if caminho.exists():
            pid = int(caminho.read_text().strip())
            if pid != os.getpid() and not _pid_vivo(pid):
                return "reinicio"
    except Exception:
        pass
    try:
        usuario = os.environ.get("USERNAME") or os.environ.get("USER")
        sessao = os.environ.get("SESSIONNAME", "")
        if usuario and sessao and sessao not in ("Services", "DISKDRIVE"):
            return "login"
    except Exception:
        pass
    return "manual"


_ESTADO_SAIDA = {"motivo": "encerramento"}


def _emitir_launcher_exit():
    """Sensor 2 (complemento): registra POR QUE o launcher saiu. Roda via
    atexit, entao so e emitido em saidas 'normais' (operador/excecao).
    Launcher morto por kill externo nao emite nada -- e isso E o sinal:
    launcher_start(motivo=reinicio) sem launcher_exit anterior."""
    try:
        diagnostico.emitir("launcher_exit", "launcher", pid=os.getpid(),
                           motivo=_ESTADO_SAIDA["motivo"])
    except Exception:
        pass


# --- Job Object (sensor 2): vinculo de vida pai-filho ----------------------
# O filho (servidor.py) e colocado num Job Object com
# JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE. Se o launcher morrer (inclusive kill
# bruto), o OS fecha os handles do launcher, o job fecha junto e o OS mata o
# filho. Assim o filho nunca sobrevive opticamente ao pai, e a morte fica
# atribuivel a 'teardown do launcher'. O handle fica guardado em _JOB_HANDLE
# (KeepHandleOpen): NUNCA o fechamos nem deixamos o GC mexer nele.

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_JOB_HANDLE = None
_JOB_TENTADO = False


def _criar_job_object():
    """Cria uma vez o Job Object. Falhou? Loga e segue: o launcher continua
    supervisionando sem o vinculo (Windows antigos / job aninhado sem
    breakaway). Nunca lanca excecao."""
    global _JOB_HANDLE, _JOB_TENTADO
    if _JOB_HANDLE is not None or _JOB_TENTADO:
        return _JOB_HANDLE
    _JOB_TENTADO = True
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            log(f"Aviso: CreateJobObjectW falhou (err {kernel32.GetLastError()}); sem vinculo job")
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            log(f"Aviso: SetInformationJobObject falhou (err {kernel32.GetLastError()}); sem vinculo job")
            kernel32.CloseHandle(handle)
            return None
        _JOB_HANDLE = handle
        log("Job Object ativo: se o launcher morrer, o OS mata o filho junto (KILL_ON_JOB_CLOSE)")
    except Exception as e:
        log(f"Aviso: job object indisponivel: {e}")
        _JOB_HANDLE = None
    return _JOB_HANDLE


def _atribuir_ao_job(proc):
    """Coloca o filho recem-nascido no job. Best-effort: se falhar (ex.: o
    launcher ja esta num job que nao permite breakaway), loga e segue."""
    try:
        if _criar_job_object() is None:
            return
        if proc is None or proc._handle is None:
            return
        kernel32 = ctypes.windll.kernel32
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        if not kernel32.AssignProcessToJobObject(_JOB_HANDLE, ctypes.c_void_p(int(proc._handle))):
            log(f"Aviso: AssignProcessToJobObject falhou (err {kernel32.GetLastError()}); filho fora do job")
    except Exception as e:
        log(f"Aviso: atribuicao ao job falhou: {e}")


# --- Event log do Windows (sensor 4c): energia / sessao / boot -------------
# Pares exatos (provedor, id) que interessam. O filtro PowerShell usa a uniao
# de provedores x a uniao de ids; a filtragem por par EXATO acontece aqui,
# porque a uniao sozinha deixaria passar ruido (ex.: Kernel-General 24, que
# e fuso-horario, nao sono).
_EVENTOS_JANELA = {
    ("Microsoft-Windows-Kernel-Power", 41): "sistema reiniciou sem desligamento limpo (ou acordou)",
    ("Microsoft-Windows-Power-Troubleshooter", 1): "sistema saiu do sono",
    ("Microsoft-Windows-Kernel-General", 12): "relogio do sistema alterado (inicio) - possivel gap de sono/boot",
    ("Microsoft-Windows-Kernel-General", 13): "relogio do sistema alterado (fim) - possivel gap de sono",
    ("EventLog", 6005): "servico de log de eventos iniciado (boot)",
    ("EventLog", 6006): "desligamento LIMPO do Windows",
    ("EventLog", 6008): "desligamento INESPERADO do Windows (queda/cut)",
    ("User32", 1074): "shutdown/restart/logoff INICIADO por usuario ou processo",
    ("Microsoft-Windows-TerminalServices-LocalSessionManager", 21): "logon de sessao",
    ("Microsoft-Windows-TerminalServices-LocalSessionManager", 22): "shell da sessao iniciado",
    ("Microsoft-Windows-TerminalServices-LocalSessionManager", 23): "LOGOFF de sessao",
    ("Microsoft-Windows-TerminalServices-LocalSessionManager", 24): "sessao CONECTADA",
    ("Microsoft-Windows-TerminalServices-LocalSessionManager", 25): "sessao DESCONECTADA",
}

_HB_FRESH_S = 10.0      # heartbeat com menos disso == matanca instantanea (kill externo)
_HB_CONGELADO_S = 30.0  # heartbeat mais velho que isso == o loop parou antes da morte


def _consultar_eventos_windows(momento_morte):
    """Eventos de energia/sessao/boot na janela [morte-90s, morte+5s], numa
    UNICA chamada a PowerShell Get-WinEvent (timeout 6s). Qualquer falha
    (sem powershell, timeout, permissao) retorna []: o launcher nunca para
    por causa deste sensor."""
    ini = (momento_morte - timedelta(seconds=90)).strftime("%Y-%m-%dT%H:%M:%S")
    fim = (momento_morte + timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%S")
    provedores = ",".join(f"'{p}'" for p in sorted({p for p, _ in _EVENTOS_JANELA}))
    ids = ",".join(str(i) for i in sorted({i for _, i in _EVENTOS_JANELA}))
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        f"$f=@{{LogName='System';StartTime=[datetime]'{ini}';EndTime=[datetime]'{fim}';"
        f"ProviderName={provedores};ID={ids}}};"
        "Get-WinEvent -FilterHashtable $f | ForEach-Object {"
        "$m=($_.Message -replace '\\s+',' '); if($m.Length -gt 160){$m=$m.Substring(0,160)};"
        "'{0}|{1}|{2}|{3}' -f $_.Id,$_.ProviderName,$_.TimeCreated.ToString('yyyy-MM-ddTHH:mm:ss'),$m }"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=6,
            encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        return []
    eventos = []
    for linha in (r.stdout or "").splitlines():
        partes = linha.strip().split("|", 3)
        if len(partes) < 3:
            continue
        try:
            id_ = int(partes[0])
        except ValueError:
            continue
        provedor = partes[1]
        if (provedor, id_) not in _EVENTOS_JANELA:
            continue  # par exato; descarta ruido da uniao provider x id
        eventos.append({
            "id": id_,
            "provedor": provedor,
            "ts": partes[2],
            "resumo": (partes[3] if len(partes) > 3 else "") or _EVENTOS_JANELA[(provedor, id_)],
        })
    return eventos


def _decidir_causa(codigo, codigo_classe, atraso_hb, eventos, encerrando):
    """Regras ORDENADAS de atribuicao (sensor 4d). A primeira que casar
    vence; a ordem reflete confianca: um evento de shutdown explicito do
    Windows explica a morte melhor que o codigo de saida, que explica
    melhor que a ausencia de tudo."""
    pares = {(e["provedor"], e["id"]) for e in eventos}
    # 6006/6008/1074 = desligamento/restart; TS-LSM 23 = LOGOFF real da
    # sessao (e este launcher vive na sessao interativa do usuario, via
    # .lnk de Startup -- logoff sem shutdown completo mata os processos da
    # sessao e hoje apareceria como "desconhecida").
    if pares & {
        ("EventLog", 6006),
        ("EventLog", 6008),
        ("User32", 1074),
        ("Microsoft-Windows-TerminalServices-LocalSessionManager", 23),
    }:
        return "shutdown/logoff do Windows"
    if pares & {
        ("Microsoft-Windows-Power-Troubleshooter", 1),
        ("Microsoft-Windows-Kernel-Power", 41),
        ("Microsoft-Windows-Kernel-General", 12),
        ("Microsoft-Windows-Kernel-General", 13),
    }:
        return "sono/energia"
    if codigo_classe.startswith("crash nativo"):
        return "crash nativo"
    if encerrando:
        return "teardown do launcher"
    if atraso_hb is not None and atraso_hb > _HB_CONGELADO_S:
        return "loop do servidor congelado (heartbeat parou antes da morte)"
    if (codigo & 0xFFFFFFFF) == 0xFFFFFFFF and atraso_hb is not None and atraso_hb <= _HB_FRESH_S:
        return "kill externo sem evento de energia/sessao (AV? taskkill?)"
    return "desconhecida"


class Supervisionado:
    def __init__(self, nome, cmd, cwd, stop_event, saude_url=None):
        self.nome = nome
        self.cmd = cmd
        self.cwd = cwd
        self.stop_event = stop_event
        self.saude_url = saude_url
        self.proc = None
        self.restarts = 0
        self.falhas_saude = 0
        self._nascido_ts = None          # sensor 4: duracao vivo
        self._ultima_resposta_ok = None  # sensor 5: idade da ultima /api/saude boa
        self.thread = threading.Thread(target=self._roda, name=nome, daemon=True)
        self._vigia_saude = None

    def iniciar(self):
        self.thread.start()
        if self.saude_url:
            self._vigia_saude = threading.Thread(target=self._vigiar_saude, name=f"{self.nome}-saude", daemon=True)
            self._vigia_saude.start()

    def _checar_saude(self):
        if not self.saude_url:
            return True
        try:
            with urllib.request.urlopen(self.saude_url, timeout=5) as resp:
                ok = resp.status == 200
                if ok:
                    self._ultima_resposta_ok = time.monotonic()
                return ok
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def _vigiar_saude(self):
        # Reinicia o processo se ele parar de responder a /api/saude,
        # mesmo estando "vivo" (trava silenciosa == site fora do ar).
        intervalo = 15
        limiar = 3
        while not self.stop_event.is_set():
            if self.stop_event.wait(intervalo):
                return
            if self.proc is None or self.proc.poll() is not None:
                self.falhas_saude = 0
                continue
            if self._checar_saude():
                self.falhas_saude = 0
                continue
            self.falhas_saude += 1
            if self.falhas_saude >= limiar:
                log(f"{self.nome}: {limiar} falhas de saude seguidas; reiniciando")
                self.falhas_saude = 0
                p = self.proc
                # Sensor 5: marca a morte COMO do watchdog ANTES de matar,
                # para diferenciar de kill externo no event log.
                try:
                    ms = None
                    if self._ultima_resposta_ok is not None:
                        ms = int((time.monotonic() - self._ultima_resposta_ok) * 1000)
                    diagnostico.emitir("watchdog_kill", "launcher", pid=p.pid,
                                       ppid=os.getpid(), falhas_saude=limiar,
                                       ultima_resposta_ms=ms)
                except Exception:
                    pass
                try:
                    p.kill()
                except Exception:
                    try:
                        p.terminate()
                    except Exception:
                        pass

    def _spawn(self):
        logf = open(self.cwd / "servidor.log", "a", encoding="utf-8", buffering=1)
        # Sensor 3: stderr do filho vai para arquivo VIVO e separado
        # (logs/servidor.stderr.log, append, linha a linha). Um traceback
        # Python de crash fica rastreavel aqui; um kill externo continua
        # silencioso -- e o silencio honesto faz parte do diagnostico.
        errf = open(LOGS / "servidor.stderr.log", "a", encoding="utf-8", buffering=1)
        logf.write(f"\n[{ts()}] === {self.nome} iniciando (tentativa #{self.restarts}) ===\n")
        logf.flush()
        env = dict(os.environ)
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        try:
            self.proc = subprocess.Popen(
                self.cmd,
                cwd=str(self.cwd),
                stdout=logf,
                stderr=errf,
                env=env,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception:
            for fh in (logf, errf):
                try:
                    fh.close()
                except Exception:
                    pass
            raise
        self._nascido_ts = time.time()
        _atribuir_ao_job(self.proc)  # sensor 2: vinculo de vida pai-filho
        log(f"{self.nome} iniciado (PID {self.proc.pid})")
        try:
            diagnostico.emitir("filho_spawn", "launcher", pid=self.proc.pid,
                               ppid=os.getpid(), cmd=list(self.cmd), nome=self.nome)
        except Exception:
            pass
        return logf, errf

    def _roda(self):
        while not self.stop_event.is_set():
            try:
                logf, errf = self._spawn()
            except Exception as e:
                log(f"ERRO ao iniciar {self.nome}: {e}")
                self._backoff()
                continue
            try:
                while self.proc.poll() is None and not self.stop_event.is_set():
                    time.sleep(0.5)
            finally:
                if self.proc is not None:
                    try:
                        codigo = self.proc.wait(timeout=5)
                    except Exception:
                        try:
                            self.proc.kill()
                        except Exception:
                            pass
                        codigo = -9
                else:
                    codigo = -1
                # Sensor 4: CAUSA NOMEADA antes de registrar a morte.
                self._diagnosticar_morte(codigo)
                try:
                    logf.write(f"[{ts()}] === {self.nome} morreu (codigo {codigo}) ===\n")
                    logf.close()
                except Exception:
                    pass
                try:
                    errf.close()
                except Exception:
                    pass
            if self.stop_event.is_set():
                break
            self.restarts += 1
            log(f"{self.nome} caiu (codigo {codigo}). Relancando com backoff...")
            self._backoff()
        log(f"{self.nome}: supervisao encerrada.")

    def _diagnosticar_morte(self, codigo):
        """Transforma 'morreu codigo -1' em causa nomeada (sensor 4):
        codigo decodificado + defasagem do heartbeat + eventos de
        energia/sessao do Windows na janela da morte, combinados pelas
        regras ordenadas de _decidir_causa. Nunca lanca excecao."""
        try:
            morte = datetime.now()
            codigo_nome, codigo_classe = diagnostico.decodificar_codigo(codigo)
            hb = diagnostico.ler_heartbeat()
            hb_ts = None
            atraso_hb = None
            if hb and hb.get("ts"):
                try:
                    hb_ts = hb["ts"]
                    atraso_hb = round(morte.timestamp() - datetime.fromisoformat(hb_ts).timestamp(), 3)
                except ValueError:
                    pass
            eventos = _consultar_eventos_windows(morte)
            encerrando = self.stop_event.is_set()
            causa = _decidir_causa(codigo, codigo_classe, atraso_hb, eventos, encerrando)
            duracao = None
            if self._nascido_ts is not None:
                duracao = round(time.time() - self._nascido_ts, 1)
            diagnostico.emitir(
                "filho_morto", "launcher",
                pid=self.proc.pid if self.proc else None,
                ppid=os.getpid(),
                codigo=codigo,
                codigo_nome=codigo_nome,
                codigo_classe=codigo_classe,
                duracao_vivo_s=duracao,
                ultimo_heartbeat_ts=hb_ts,
                heartbeat_atraso_s=atraso_hb,
                causa_provavel=causa,
                eventos_windows=eventos,
            )
            log(f"{self.nome}: causa provavel da morte: {causa} "
                f"(codigo {codigo} = {codigo_nome}; heartbeat atraso {atraso_hb}s; "
                f"{len(eventos)} evento(s) Windows na janela)")
        except Exception as e:
            log(f"Aviso: diagnostico de morte falhou: {e}")

    def _backoff(self):
        espera = BACKOFF_STEPS[min(self.restarts, len(BACKOFF_STEPS) - 1)]
        fim = time.time() + espera
        while time.time() < fim and not self.stop_event.is_set():
            time.sleep(0.2)

    def parar(self):
        p = self.proc
        if p and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass


def _keep_awake():
    try:
        import ctypes

        ctypes.windll.kernel32.SetThreadExecutionState(0x80000002)
        log("KeepAwake: computador nao vai dormir enquanto os monitores rodarem")
    except Exception as e:
        log(f"Aviso: KeepAwake falhou: {e}")


def _prioridade_alta():
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x80)
        log("Prioridade do processo: ALTA")
    except Exception as e:
        log(f"Aviso: prioridade alta falhou: {e}")


def _urls_lan():
    import socket

    ips = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    linhas = []
    for ip in ips:
        linhas.append(f"  gemini: http://{ip}:8011")
    return linhas


def main():
    os.chdir(str(BASE))
    LOGS.mkdir(parents=True, exist_ok=True)

    if _ja_rodando():
        log("Outro launcher ja esta rodando. Encerrando esta instancia.")
        return 0

    # Sensor 1: por que este launcher comecou? (boot/login/manual/reinicio)
    try:
        motivo = _motivo_start()
        diagnostico.emitir("launcher_start", "launcher", pid=os.getpid(), motivo=motivo)
        log(f"launcher_start motivo={motivo}")
        atexit.register(_emitir_launcher_exit)
    except Exception as e:
        log(f"Aviso: sensor de start falhou: {e}")

    _criar_job_object()  # sensor 2: se der erro aqui, o launcher segue normal

    _keep_awake()
    _prioridade_alta()

    (BASE / "launcher.pid").write_text(str(os.getpid()))

    stop_event = threading.Event()
    # RUA ÚNICA: este launcher é dono SÓ da 8011 (gateway Google). A 8010
    # (opencode) pertence ao launcher do Kwai Editor (construção), que a
    # supervisiona e relança — dois donos na mesma porta geravam disputa.
    specs = [
        ("gemini", [sys.executable, "servidor.py"], BASE, "http://127.0.0.1:8011/api/saude"),
    ]
    supervisores = []
    for nome, cmd, cwd, saude in specs:
        if _porta_ja_atendida(saude):
            log(f"{nome}: ja existe servidor respondendo em {saude}; nao vou iniciar outro")
            continue
        s = Supervisionado(nome, cmd, cwd, stop_event, saude_url=saude)
        s.iniciar()
        supervisores.append(s)

    log("Monitores supervisionados: gemini (8011)")
    for linha in _urls_lan():
        log(linha)
    log("Qualquer servidor que cair sera relancado automaticamente (backoff 2/5/10/30s).")
    log(f"Logs: {LOGS / 'launcher.log'}")

    try:
        while not stop_event.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        _ESTADO_SAIDA["motivo"] = "operador"
        log("Encerrando por decisao do operador...")
    finally:
        stop_event.set()
        for s in supervisores:
            s.parar()
        (BASE / "launcher.pid").unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
