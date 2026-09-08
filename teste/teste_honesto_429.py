import itertools
import os
import tempfile
import unittest
from pathlib import Path

PASTA_TEMP = Path(tempfile.mkdtemp(prefix="monitor-honesto-"))

os.environ["GEMINI_API_BASE"] = "http://127.0.0.1:1"

from app import db
from app.endpoints import _sem_chave
from app.proxy import SemChaveDisponivel

_contador = itertools.count()


class TestHonesto429(unittest.TestCase):
    def setUp(self):
        self.db_path = PASTA_TEMP / f"honesto-{next(_contador)}.db"
        os.environ["MONITOR_DB"] = str(self.db_path)
        db.inicializar()

    def test_registrar_honesto_escreve_e_conta(self):
        self.assertEqual(db.contar_honestos(), 0)
        db.registrar_honesto("onda: fila cheia; proxima janela em 12s", 12, 0)
        self.assertEqual(db.contar_honestos(), 1)
        db.registrar_honesto("onda: outro", 5, 0)
        self.assertEqual(db.contar_honestos(), 2)

    def test_sem_chave_com_onda_registra_e_retorna_429(self):
        resp = _sem_chave(SemChaveDisponivel("onda: teste", 7))
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.headers.get("Retry-After"), "7")
        self.assertEqual(db.contar_honestos(), 1)

    def test_sem_chave_sem_onda_nao_registra(self):
        resp = _sem_chave(SemChaveDisponivel("tudo em cooldown", None))
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(db.contar_honestos(), 0)


if __name__ == "__main__":
    unittest.main()
