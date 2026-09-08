import json
import os
import tempfile
import unittest
from pathlib import Path

PASTA_TEMP = Path(tempfile.mkdtemp(prefix="monitor-mudos-"))

os.environ["GEMINI_API_BASE"] = "http://127.0.0.1:1"

from app import db, gemini_api, proxy
from app.modelos import _util_para_chat

# Corpo real de erro que o Google entregou (historico.db, 2026-08-23):
# dados verdadeiros de producao, nao fabricados.
CORPO_GOOGLE_REAL = json.dumps({
    "error": {
        "code": 400,
        "message": (
            "Function call is missing a thought_signature in functionCall parts. "
            "This is required for tools to work correctly. Please refer to "
            "https://ai.google.dev/gemini-api/docs/thought-signatures for more details."
        ),
    }
}).encode("utf-8")


class TestDescricaoNuncaMuda(unittest.TestCase):
    def test_sem_corpo_diz_sem_corpo(self):
        desc = gemini_api.descricao_erro(400, b"")
        self.assertIn("HTTP 400", desc)
        self.assertIn("sem corpo", desc)

    def test_corpo_ilegivel_diz_corpo_ilegivel(self):
        desc = gemini_api.descricao_erro(404, b"<html>not found</html>")
        self.assertIn("corpo ilegivel", desc)

    def test_corpo_real_do_google_vira_mensagem_completa(self):
        desc = gemini_api.descricao_erro(400, CORPO_GOOGLE_REAL)
        self.assertIn("400 (HTTP 400)", desc)
        self.assertIn("thought_signature", desc)


class TestAssinaturasEmDisco(unittest.TestCase):
    def setUp(self):
        os.environ["MONITOR_DB"] = str(PASTA_TEMP / f"assig-{self.id().split('.')[-1]}.db")
        db.inicializar()

    def test_assinatura_registrada_pelo_corpo_de_resposta_sobrevive_e_reinjeta(self):
        corpo_resp = json.dumps({"choices": [{"message": {"tool_calls": [
            {"id": "call_abc", "extra_content": {"google": {"thought_signature": "SIG-REAL"}}},
        ]}}]}).encode("utf-8")
        proxy.registrar_assinaturas_de_resposta(corpo_resp)
        pedido = json.dumps({"messages": [
            {"role": "assistant", "tool_calls": [
                {"id": "call_abc", "type": "function",
                 "function": {"name": "bash", "arguments": "{}"}},
            ]},
        ]}).encode("utf-8")
        saida = json.loads(proxy.injetar_assinaturas(pedido))
        tc = saida["messages"][0]["tool_calls"][0]
        self.assertEqual(tc["extra_content"]["google"]["thought_signature"], "SIG-REAL")

    def test_assinatura_vem_do_disco_nao_da_ram(self):
        # A prova de que nao ha estado em memoria: gravar via db, zerar
        # qualquer referencia, e ler por uma conexao nova (o modulo proxy nao
        # guarda mais nada alem do SQLite).
        db.gravar_assinatura("call_xyz", "SIG-DISCO")
        self.assertEqual(proxy._buscar_assinatura("call_xyz"), "SIG-DISCO")

    def test_chunk_de_stream_registra_assinatura(self):
        linha = b'data: {"choices":[{"delta":{"tool_calls":[{"id":"call_s1","extra_content":{"google":{"thought_signature":"SIG-STREAM"}}}]}}]}\n'
        proxy.registrar_assinatura_de_chunk(linha)
        self.assertEqual(db.ler_assinatura("call_s1"), "SIG-STREAM")

    def test_injetar_preserva_assinatura_que_o_cliente_ja_ecoou(self):
        pedido = json.dumps({"messages": [
            {"role": "assistant", "tool_calls": [
                {"id": "call_nada", "extra_content": {"google": {"thought_signature": "DO-CLIENTE"}},
                 "function": {"name": "x", "arguments": "{}"}},
            ]},
        ]}).encode("utf-8")
        saida = proxy.injetar_assinaturas(pedido)
        self.assertIn("DO-CLIENTE", saida.decode("utf-8"))


class TestErroUpstreamETipoProprio(unittest.TestCase):
    def test_erro_de_repasse_carrega_status_corpo_e_desc(self):
        erro = proxy.ErroUpstream(400, CORPO_GOOGLE_REAL, "Google-1", 42, "400 (HTTP 400) | msg")
        self.assertEqual(erro.status, 400)
        self.assertEqual(erro.corpo, CORPO_GOOGLE_REAL)
        self.assertEqual(erro.nome, "Google-1")
        self.assertIsInstance(erro, Exception)

    def test_nao_existe_mais_resposta_falsa_polimorfica(self):
        # O bug original: um objeto-erro que fingia ser resposta e o caminho
        # de stream tentava iterar. Se a classe voltar, o tipo de erro proprio
        # deve continuar existindo junto com o teste acima.
        self.assertFalse(hasattr(proxy, "_RespErro"))
        self.assertTrue(issubclass(proxy.ErroUpstream, Exception))


class TestAprendizadoDeModeloMorto(unittest.TestCase):
    def setUp(self):
        self.cfg_path = PASTA_TEMP / f"cfg-{self.id().split('.')[-1]}.json"
        os.environ["MONITOR_CONFIG"] = str(self.cfg_path)
        self.cfg = {"modelos": ["gemini-2.5-flash-lite", "gemini-3.7-flash"],
                    "modelos_indisponiveis": []}
        self.cfg_path.write_text(json.dumps(self.cfg), encoding="utf-8")

    def test_404_tira_da_lista_e_persiste_em_disco(self):
        proxy.aprender_modelo_morto(self.cfg, "gemini-2.5-flash-lite")
        self.assertNotIn("gemini-2.5-flash-lite", self.cfg["modelos"])
        self.assertIn("gemini-2.5-flash-lite", self.cfg["modelos_indisponiveis"])
        salvo = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertIn("gemini-2.5-flash-lite", salvo["modelos_indisponiveis"])

    def test_aprender_duas_vezes_nao_duplica(self):
        proxy.aprender_modelo_morto(self.cfg, "gemini-2.5-flash-lite")
        proxy.aprender_modelo_morto(self.cfg, "gemini-2.5-flash-lite")
        self.assertEqual(self.cfg["modelos_indisponiveis"].count("gemini-2.5-flash-lite"), 1)

    def test_sync_nao_ressuscita_modelo_aprendido(self):
        proxy.aprender_modelo_morto(self.cfg, "gemini-2.5-flash-lite")
        mortos = set(self.cfg["modelos_indisponiveis"])
        self.assertFalse(_util_para_chat("gemini-2.5-flash-lite", mortos))
        self.assertTrue(_util_para_chat("gemini-3.7-flash", mortos))


if __name__ == "__main__":
    unittest.main()
