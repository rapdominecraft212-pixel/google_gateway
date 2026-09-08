import json
import logging
import os
import sqlite3
import stat
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta

from . import PASTA, janelas
from .config import caminho_db

try:
    import diagnostico
except ImportError:  # execucao a partir de fora da raiz: garante o contrato no path
    import sys

    sys.path.insert(0, str(PASTA))
    import diagnostico

log = logging.getLogger("monitor.db")


def conectar():
    caminho = caminho_db()
    caminho.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(caminho, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


# --- conexao-ancora: fecha a janela da corrida de -wal/-shm -----------------
# Causa-raiz medida em 2026-09-06 (vigia de 2ms + 5342 eventos desde 23/08):
# com o padrao abre/fecha por operacao, quando a ULTIMA conexao fecha o SQLite
# faz checkpoint e APAGA -wal/-shm; uma escrita que abre conexao nessa janela
# (<100ms, rajadas de 8-28 falhas no mesmo segundo) recebe
# SQLITE_READONLY ("attempt to write a readonly database") e era descartada.
# A ancora mantem UMA conexao aberta para sempre: a "ultima conexao" nunca
# fecha, o WAL nunca e deletado, e a corrida deixa de existir. Conexao ociosa
# (sem transacao) NAO bloqueia checkpoint nem escrita concorrente em WAL.
_ANCORA = None  # (caminho, conexao)
_ANCORA_LOCK = threading.Lock()


def _ancorar():
    global _ANCORA
    caminho = str(caminho_db())
    with _ANCORA_LOCK:
        if _ANCORA is not None and _ANCORA[0] == caminho:
            return
        if _ANCORA is not None:
            try:
                _ANCORA[1].close()
            except Exception:
                pass
        _ANCORA = (caminho, conectar())


def _reancorar_se_morta():
    """True se recriou a ancora (estava morta ou apontava para outro arquivo).
    Usado no retry: se o banco foi movido por fora, a ancora antiga e o lixo."""
    global _ANCORA
    caminho = str(caminho_db())
    with _ANCORA_LOCK:
        if _ANCORA is not None and _ANCORA[0] == caminho:
            try:
                _ANCORA[1].execute("SELECT 1").fetchone()
                return False
            except sqlite3.Error:
                pass
            try:
                _ANCORA[1].close()
            except Exception:
                pass
        _ANCORA = (caminho, conectar())
        return True


def inicializar():
    with closing(conectar()) as con, con:
        colunas = {linha[1] for linha in con.execute("PRAGMA table_info(snapshots)").fetchall()}
        if colunas and "rolling_pct" in colunas:
            con.execute("DROP TABLE snapshots")
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                quando TEXT NOT NULL,
                chave TEXT NOT NULL,
                minuto_pct REAL,
                dia_pct REAL,
                mes_pct REAL,
                minuto_reset TEXT,
                dia_reset TEXT,
                mes_reset TEXT,
                raw TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_snap_chave ON snapshots(chave, id);

            CREATE TABLE IF NOT EXISTS requisicoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                quando TEXT NOT NULL,
                chave TEXT NOT NULL,
                modelo TEXT,
                ok INTEGER NOT NULL,
                status INTEGER,
                ms INTEGER,
                tokens_in INTEGER DEFAULT 0,
                tokens_out INTEGER DEFAULT 0,
                erro TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_req_id ON requisicoes(id);

            CREATE TABLE IF NOT EXISTS honest_429s (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                quando TEXT NOT NULL,
                motivo TEXT NOT NULL,
                retry_after INTEGER,
                estimativa_tokens INTEGER
            );

            CREATE TABLE IF NOT EXISTS assinaturas (
                call_id TEXT PRIMARY KEY,
                sig TEXT NOT NULL,
                atualizado_em REAL NOT NULL
            );
            """
        )
    _ancorar()


def _agora():
    return datetime.now().isoformat(timespec="seconds")


def escrevivel():
    """Sonda real de escrevibilidade: INSERT de sentinela dentro de uma
    transacao com ROLLBACK — nao deixa rastro nem corrompe dados. Usada pelo
    heartbeat e por /api/saude/detalhe para distinguir 'banco so le' de
    'banco travado' sem depender de log."""
    try:
        caminho = caminho_db()
        caminho.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(caminho), check_same_thread=False, isolation_level=None)
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "INSERT INTO honest_429s (quando, motivo, retry_after, estimativa_tokens) VALUES (?,?,?,?)",
                ("sonda", "__sonda_escrevibilidade__", 0, 0),
            )
            con.execute("ROLLBACK")
        finally:
            con.close()
        return True
    except sqlite3.Error:
        return False


_ultimo_db_readonly_emitido = 0.0


def _emitir_db_readonly_causa(erro, tentativas):
    """Causa nomeada do ramo 'banco somente-leitura' de _escrever. A condicao
    dispara em rajada (toda escrita falha), entao o envio e limitado a 1x a
    cada 60s para nao inundar o JSONL. Nunca lanca: diagnostico nao pode
    derrubar a escrita que esta sendo diagnosticada."""
    global _ultimo_db_readonly_emitido
    agora = time.time()
    if agora - _ultimo_db_readonly_emitido < 60:
        return
    _ultimo_db_readonly_emitido = agora
    try:
        caminho = caminho_db()
        sem_permissao = True
        atributos = 0
        try:
            sem_permissao = not os.access(caminho, os.W_OK)
            atributos = getattr(os.stat(caminho), "st_file_attributes", 0) or 0
        except OSError:
            pass
        somente_leitura_flag = bool(
            sem_permissao or (atributos & getattr(stat, "FILE_ATTRIBUTE_READONLY", 0))
        )
        diagnostico.emitir(
            "db_readonly_causa",
            "servidor",
            erro=str(erro),
            caminho=str(caminho),
            somente_leitura_flag=somente_leitura_flag,
            dir_w_ok=os.access(str(caminho.parent), os.W_OK),
            tentativas=tentativas,
        )
    except Exception:
        pass


def _escrever(operacao, tentativas=5):
    # Nao podemos perder nenhum registro de requisicao: se perder, o contador de
    # cota fica defasado (restante superestimado) e o usuario estoura a cota sem
    # saber. Por isso retentamos transientes em vez de descartar a gravacao:
    # "database is locked" (concorrente segurando o write lock) e
    # "attempt to write a readonly database" (corrida de -wal/-shm medida em
    # 2026-09-06: rajadas de <100ms em que a ultima conexao fechada deletava
    # o WAL no instante de uma abertura concorrente). A conexao-ancora fecha a
    # janela; o retry e a defesa em profundidade para o resto.
    _ancorar()
    erro_final = None
    for tentativa in range(tentativas):
        try:
            with closing(conectar()) as con:
                operacao(con)
                con.commit()
            return
        except sqlite3.OperationalError as erro:
            msg = str(erro).lower()
            if "locked" in msg:
                erro_final = erro
                time.sleep(0.05 * (tentativa + 1))
                continue
            if "readonly" in msg:
                erro_final = erro
                _reancorar_se_morta()
                time.sleep(0.05 * (tentativa + 1))
                continue
            log.warning("escrita no banco ignorada: %s", erro)
            return
        except sqlite3.Error as erro:
            log.warning("escrita no banco falhou: %s", erro)
            return
    if erro_final is not None:
        if "readonly" in str(erro_final).lower():
            log.warning("escrita no banco ignorada (banco somente-leitura?): %s", erro_final)
            _emitir_db_readonly_causa(erro_final, tentativas)
        else:
            log.warning("escrita no banco falhou apos %d tentativas (locked): %s", tentativas, erro_final)


def registrar_snapshot(chave, janelas, raw):
    def _op(con):
        con.execute(
            """INSERT INTO snapshots
               (quando, chave, minuto_pct, dia_pct, mes_pct,
                minuto_reset, dia_reset, mes_reset, raw)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                _agora(),
                chave,
                _pct(janelas, "minuto"),
                _pct(janelas, "dia"),
                _pct(janelas, "mes"),
                _reset(janelas, "minuto"),
                _reset(janelas, "dia"),
                _reset(janelas, "mes"),
                json.dumps(raw, ensure_ascii=False) if raw is not None else None,
            ),
        )
    _escrever(_op)


def registrar_requisicao(chave, modelo, ok, status=None, ms=None, tokens_in=0, tokens_out=0, erro=None):
    ultimo = [None]

    def _op(con):
        cursor = con.execute(
            """INSERT INTO requisicoes (quando, chave, modelo, ok, status, ms, tokens_in, tokens_out, erro)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (_agora(), chave, modelo, 1 if ok else 0, status, ms, tokens_in or 0, tokens_out or 0, erro),
        )
        ultimo[0] = cursor.lastrowid
    _escrever(_op)
    return ultimo[0]


def atualizar_requisicao(req_id, **campos):
    permitidos = {"status", "ms", "tokens_in", "tokens_out", "erro"}
    for campo in list(campos):
        if campo not in permitidos:
            campos.pop(campo)
    if not campos:
        return

    def _op(con):
        pares = ", ".join(f"{c} = ?" for c in campos)
        con.execute(f"UPDATE requisicoes SET {pares} WHERE id = ?", (*campos.values(), req_id))
    _escrever(_op)


def ultima_chave_usada():
    """Retorna o nome da ultima chave que completou requisicao com sucesso,
    ou None se o banco estiver vazio / sem historico (memoria persistente do anel)."""
    try:
        with closing(conectar()) as con:
            row = con.execute(
                "SELECT chave FROM requisicoes WHERE ok = 1 ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row and row["chave"]:
                return str(row["chave"]).strip()
    except Exception:
        pass
    return None


def registrar_honesto(motivo, retry_after=None, estimativa_tokens=0):
    def _op(con):
        con.execute(
            """INSERT INTO honest_429s (quando, motivo, retry_after, estimativa_tokens)
               VALUES (?,?,?,?)""",
            (_agora(), motivo, retry_after, estimativa_tokens or 0),
        )
    _escrever(_op)


def contar_honestos():
    with closing(conectar()) as con, con:
        linha = con.execute("SELECT COUNT(*) AS n FROM honest_429s").fetchone()
    return linha["n"] or 0


_IDADE_ASSINATURA_MAX_SEG = 7 * 86400


def gravar_assinatura(call_id, sig):
    """thought_signature de um functionCall, persistida em disco. O OpenAI
    format nao transporta a assinatura de volta ao Google; o gateway e a
    memoria da conversa. Em RAM ela morria no reinício/evicao e o Google
    devolvia 400; em SQLite sobrevive a tudo que o processo faz."""
    agora = time.time()

    def _op(con):
        con.execute(
            """INSERT INTO assinaturas (call_id, sig, atualizado_em) VALUES (?,?,?)
               ON CONFLICT(call_id) DO UPDATE
                 SET sig = excluded.sig, atualizado_em = excluded.atualizado_em""",
            (call_id, sig, agora),
        )
        con.execute("DELETE FROM assinaturas WHERE atualizado_em < ?", (agora - _IDADE_ASSINATURA_MAX_SEG,))

    _escrever(_op)


def ler_assinatura(call_id):
    try:
        with closing(conectar()) as con, con:
            linha = con.execute(
                "SELECT sig FROM assinaturas WHERE call_id = ?", (call_id,)
            ).fetchone()
        return linha["sig"] if linha else None
    except sqlite3.Error:
        return None


def _pct(janelas, nome):
    jan = (janelas or {}).get(nome) or {}
    valor = jan.get("percent")
    return float(valor) if valor is not None else None


def _reset(janelas, nome):
    jan = (janelas or {}).get(nome) or {}
    return jan.get("resetsAt")


def ok_ultimos_seg(chave, seg=60, agora=None):
    agora = agora or datetime.now()
    desde = (agora - timedelta(seconds=seg)).isoformat(timespec="seconds")
    with closing(conectar()) as con, con:
        linha = con.execute(
            "SELECT COUNT(*) AS n FROM requisicoes WHERE chave = ? AND ok = 1 AND quando >= ?",
            (chave, desde),
        ).fetchone()
    return linha["n"] or 0


def tokens_ultimos_seg(chave, seg=60, agora=None):
    agora = agora or datetime.now()
    desde = (agora - timedelta(seconds=seg)).isoformat(timespec="seconds")
    with closing(conectar()) as con, con:
        linha = con.execute(
            """SELECT COALESCE(SUM(CASE WHEN ok = 1 THEN tokens_in + tokens_out ELSE 0 END), 0) AS n
               FROM requisicoes
               WHERE chave = ? AND quando >= ?""",
            (chave, desde),
        ).fetchone()
    return linha["n"] or 0


def uso_bruto(chave, agora=None):
    agora = agora or datetime.now()
    desde_minuto = (agora - timedelta(seconds=60)).isoformat(timespec="seconds")

    def contar(desde):
        with closing(conectar()) as con, con:
            linha = con.execute(
                """SELECT COUNT(*) AS reqs,
                          COALESCE(SUM(CASE WHEN ok = 1 THEN tokens_in + tokens_out ELSE 0 END), 0) AS tokens,
                          MIN(quando) AS primeiro
                   FROM requisicoes
                   WHERE chave = ? AND ok = 1 AND quando >= ?""",
                (chave, desde),
            ).fetchone()
        return {"reqs": linha["reqs"] or 0, "tokens": linha["tokens"] or 0, "primeiro": linha["primeiro"]}

    return {
        "minuto": contar(desde_minuto),
        "dia": contar(janelas.desde_dia_iso(agora)),
        "mes": contar(janelas.desde_mes_iso(agora)),
    }


def uso_dia_ok_por_chave(agora=None):
    agora = agora or datetime.now()
    desde = janelas.desde_dia_iso(agora)
    with closing(conectar()) as con, con:
        linhas = con.execute(
            """SELECT chave, COUNT(*) AS reqs
               FROM requisicoes
               WHERE ok = 1 AND quando >= ?
               GROUP BY chave""",
            (desde,),
        ).fetchall()
    return {l["chave"]: l["reqs"] for l in linhas}


def historico_uso(chave=None, limite=200):
    with closing(conectar()) as con, con:
        if chave:
            linhas = con.execute(
                "SELECT * FROM snapshots WHERE chave = ? ORDER BY id DESC LIMIT ?", (chave, limite)
            ).fetchall()
        else:
            linhas = con.execute("SELECT * FROM snapshots ORDER BY id DESC LIMIT ?", (limite,)).fetchall()
    return [dict(l) for l in linhas]


def historico_requisicoes(limite=100):
    with closing(conectar()) as con, con:
        linhas = con.execute("SELECT * FROM requisicoes ORDER BY id DESC LIMIT ?", (limite,)).fetchall()
    return [dict(l) for l in linhas]


def balanco_recente_por_chave(seg=600, agora=None):
    """Ok/falhas de cada chave nos ultimos `seg` segundos (janela movel).

    Diferente do estado em memoria (que o gateway limpa a cada coleta de uso),
    o banco conserva o historico real das requisicoes — entao a UI consegue
    mostrar uma chave como instavel mesmo depois de o cooldown ter zerado.
    """
    agora = agora or datetime.now()
    desde = (agora - timedelta(seconds=seg)).isoformat(timespec="seconds")
    with closing(conectar()) as con, con:
        linhas = con.execute(
            """SELECT chave,
                      SUM(ok) AS ok,
                      SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS falhas
               FROM requisicoes
               WHERE quando >= ?
               GROUP BY chave""",
            (desde,),
        ).fetchall()
    return {l["chave"]: {"ok": l["ok"] or 0, "falhas": l["falhas"] or 0} for l in linhas}


def requisicoes_por_chave_hoje():
    hoje = datetime.now().strftime("%Y-%m-%d")
    with closing(conectar()) as con, con:
        linhas = con.execute(
            """SELECT chave,
                      SUM(ok) AS ok,
                      SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS falhas,
                      COUNT(*) AS total
               FROM requisicoes
               WHERE substr(quando, 1, 10) = ?
               GROUP BY chave""",
            (hoje,),
        ).fetchall()
    return {l["chave"]: dict(l) for l in linhas}


def estatisticas():
    com = conectar()
    try:
        total = com.execute(
            "SELECT COUNT(*) AS total, SUM(ok) AS ok, SUM(tokens_in) AS tin, SUM(tokens_out) AS tout FROM requisicoes"
        ).fetchone()
        hoje = com.execute(
            """SELECT COUNT(*) AS total, SUM(ok) AS ok, SUM(tokens_in) AS tin, SUM(tokens_out) AS tout
               FROM requisicoes WHERE substr(quando, 1, 10) = ?""",
            (datetime.now().strftime("%Y-%m-%d"),),
        ).fetchone()
        media_ms = com.execute("SELECT AVG(ms) AS media FROM requisicoes WHERE ok = 1").fetchone()
        return {
            "total": dict(total),
            "hoje": dict(hoje),
            "media_ms_ok": media_ms["media"],
        }
    finally:
        com.close()
