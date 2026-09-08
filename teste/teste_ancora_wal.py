import itertools
import os
import sqlite3
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path

PASTA_TEMP = Path(tempfile.mkdtemp(prefix="monitor-ancora-"))

os.environ["GEMINI_API_BASE"] = "http://127.0.0.1:1"

from app import db

_contador = itertools.count()


class TestAncoraWal(unittest.TestCase):
    """Prova da correcao da corrida -wal/-shm (causa-raiz medida 2026-09-06):
    a conexao-ancora mantem os arquivos do WAL vivos entre escritas, rajadas
    concorrentes nao perdem nenhuma gravacao, e uma condicao REAL de readonly
    (atributo do arquivo) e tratada como transiente com recuperacao sozinho."""

    def setUp(self):
        self.db_path = PASTA_TEMP / f"ancora-{next(_contador)}.db"
        os.environ["MONITOR_DB"] = str(self.db_path)
        db.inicializar()

    def test_ancora_mantem_wal_vivo_entre_escritas(self):
        db.registrar_honesto("sonda", 1, 0)
        wal = Path(str(self.db_path) + "-wal")
        shm = Path(str(self.db_path) + "-shm")
        self.assertTrue(
            wal.exists() or shm.exists(),
            "WAL nao persistiu apos a escrita: a ancora nao esta segurando o banco",
        )

    def test_rajada_concorrente_nao_perde_escrita(self):
        # Padrao do poller real: 16 workers escrevendo, abre/fecha por operacao.
        n_threads, por_thread = 16, 40

        def escritora(tid):
            for i in range(por_thread):
                db.registrar_snapshot(f"c{tid}", {"minuto": {"percent": float(i)}}, None)
                time.sleep(0.001)

        threads = [threading.Thread(target=escritora, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        con = sqlite3.connect(self.db_path)
        try:
            gravadas = con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(gravadas, n_threads * por_thread)

    def test_readonly_real_e_transiente_com_recuperacao(self):
        # Condicao REAL de readonly via atributo do arquivo (sem mock):
        # _escrever deve retentar (gasta o orcamento), descartar sem subir
        # excecao ao chamador, e RECUPERAR sozinho quando a condicao passa.
        os.chmod(self.db_path, stat.S_IREAD)
        try:
            t0 = time.time()
            db.registrar_honesto("durante-readonly", 1, 0)
            gastou = time.time() - t0
        finally:
            os.chmod(self.db_path, stat.S_IREAD | stat.S_IWRITE)
        self.assertGreaterEqual(gastou, 0.3, "nao passou pelo loop de retry")
        self.assertEqual(db.contar_honestos(), 0)
        db.registrar_honesto("pos-recuperacao", 1, 0)
        self.assertEqual(db.contar_honestos(), 1)

    def test_troca_de_caminho_reancora(self):
        # Testes (e MONITOR_DB) trocam o arquivo; a ancora tem que acompanhar.
        db.registrar_honesto("no-antigo", 1, 0)
        outro = PASTA_TEMP / f"outro-{next(_contador)}.db"
        os.environ["MONITOR_DB"] = str(outro)
        db.inicializar()
        db.registrar_honesto("no-novo", 1, 0)
        con = sqlite3.connect(outro)
        try:
            n = con.execute("SELECT COUNT(*) FROM honest_429s").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
