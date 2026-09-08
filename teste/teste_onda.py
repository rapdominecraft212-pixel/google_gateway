import json
import time
import unittest


def _cfg_base(**over):
    cfg = {
        "reasoning_effort": "",
        "max_tokens_padrao": 0,
        "timeout_http_seg": 5,
        "cooldown_429_seg": 5,
        "cooldown_429_max_seg": 60,
        "cooldown_5xx_seg": 2,
        "espera_cooldown_max_seg": 2,
        "chaves_por_tentativa": 1,
        "max_ciclos_espera": 3,
        "tentativas_por_pedido": 5,
        "max_espera_requisicao_seg": 5,
        "max_conc_por_key": 2,
        "chaves": [{"nome": "G1", "key": "K1", "tipo": "gemini"}],
        "limites": {"rpm": 1000, "tpm": 1000000, "requisicoes_dia": 1500,
                    "tokens_dia": 1000000, "tokens_mes": 20000000},
        "onda_limite_tokens": 10,
        "onda_max_na_fila": 48,
        "onda_max_espera_seg": 30,
        "onda_jitter_max_ms": 0,
        "onda_adianto_ms": 0,
    }
    cfg.update(over)
    return cfg


def _corpo_grande(n=400):
    return json.dumps({"model": "m", "messages": [{"role": "user", "content": "x" * n}]}).encode("utf-8")


class TestPuras(unittest.TestCase):
    def test_bucket_id(self):
        from app.onda import bucket_id
        self.assertEqual(bucket_id(0), 0)
        self.assertEqual(bucket_id(59), 0)
        self.assertEqual(bucket_id(60), 1)
        self.assertEqual(bucket_id(61.5), 1)
        self.assertEqual(bucket_id(120), 2)
        # sem arg usa relogio real
        self.assertEqual(bucket_id(), int(time.time() // 60))

    def test_next_wave_ts(self):
        from app.onda import next_wave_ts
        self.assertEqual(next_wave_ts(61.5, 0), 120)
        self.assertEqual(next_wave_ts(0, 0), 60)
        self.assertEqual(next_wave_ts(60, 0), 120)
        self.assertAlmostEqual(next_wave_ts(61.5, 250), 120.25)
        self.assertAlmostEqual(next_wave_ts(61.5, 0), 120.0)
        # fronteira exata: proxima, nunca a atual
        self.assertGreater(next_wave_ts(time.time(), 0), time.time())

    def test_jitter_bounds(self):
        from app.onda import jitter_seg
        self.assertEqual(jitter_seg(0), 0.0)
        self.assertEqual(jitter_seg(None), 0.0)
        for _ in range(200):
            v = jitter_seg(2000)
            self.assertGreaterEqual(v, 0.0)
            self.assertLessEqual(v, 2.0 + 1e-9)
        for _ in range(50):
            v = jitter_seg(500)
            self.assertLessEqual(v, 0.5 + 1e-9)


class TestLease(unittest.TestCase):
    def test_lease_bloqueia_e_libera(self):
        from app.estado import Estado
        from app.onda import WaveScheduler, bucket_id
        estado = Estado()
        cfg = _cfg_base(onda_limite_tokens=10,
                        onda_jitter_max_ms=0, onda_adianto_ms=0,
                        onda_max_espera_seg=70, onda_max_na_fila=48)
        sched = WaveScheduler(estado, cfg)
        self.assertFalse(sched.minuto_ocupado("G1"))
        # claim atomico: 1o ganha, 2o perde (corrida na mesma chave/minuto)
        self.assertTrue(sched.reivindicar("G1"))
        self.assertTrue(sched.minuto_ocupado("G1"))
        self.assertFalse(sched.reivindicar("G1"), "2o claim no mesmo minuto deve perder")
        # outra chave nao e afetada (pacing e POR CHAVE, nao global)
        self.assertFalse(sched.minuto_ocupado("G2"))
        self.assertTrue(sched.reivindicar("G2"))
        self.assertGreaterEqual(sched.depth(), 2)
        # liberar_minuto solta sem dispatch (claim sem fire)
        sched.liberar_minuto("G2")
        self.assertFalse(sched.minuto_ocupado("G2"))
        self.assertTrue(sched.reivindicar("G2"))
        # minuto seguinte libera o lease antigo
        balde_atual = bucket_id(time.time())
        with sched._lock:
            sched._disparos["G1"] = balde_atual - 1
        self.assertFalse(sched.minuto_ocupado("G1"))
        self.assertTrue(sched.reivindicar("G1"))


class TestConfig(unittest.TestCase):
    def test_leitura_aceita_top_e_aninhado(self):
        from app.onda import WaveScheduler
        from app.estado import Estado
        # top-level flat (canonico)
        s1 = WaveScheduler(Estado(), {"onda_limite_tokens": 5})
        self.assertTrue(s1.deve_agendar(10))
        self.assertFalse(s1.deve_agendar(4))
        # aninhado com prefixo
        s2 = WaveScheduler(Estado(), {"onda": {"onda_limite_tokens": 5}})
        self.assertTrue(s2.deve_agendar(10))
        # aninhado curto (sem prefixo)
        s3 = WaveScheduler(Estado(), {"onda": {"limite_tokens": 5}})
        self.assertTrue(s3.deve_agendar(10))
        # default do threshold quando ausente: 200000
        s4 = WaveScheduler(Estado(), {})
        self.assertTrue(s4.deve_agendar(10 ** 9))
        self.assertFalse(s4.deve_agendar(1000))


class TestSempreLigado(unittest.TestCase):
    def test_pedido_pequeno_nunca_agenda(self):
        # Sem flag on/off: o que decide e o tamanho. Pequeno passa direto em
        # qualquer config; threshold absurdo equivale a pacing desligado.
        from app.estado import Estado
        from app.onda import WaveScheduler
        s_abs = WaveScheduler(Estado(), _cfg_base(onda_limite_tokens=10 ** 12))
        for est in (1, 100, 50000, 300000):
            self.assertFalse(s_abs.deve_agendar(est))
        self.assertFalse(s_abs.minuto_ocupado("G1"))
        self.assertEqual(s_abs.depth(), 0)
    def test_proxy_corpo_pequeno_sem_efeito_onda(self):
        # Corpo pequeno (est < limite) nunca entra no pacing: falha rapida de
        # roteamento sem rede e sem prefixo "onda:".
        from app.estado import Estado
        from app import proxy as proxy_mod
        corpo = _corpo_grande(10)  # pequeno
        cfg = _cfg_base(onda_limite_tokens=10,
                        chaves=[], max_espera_requisicao_seg=5)
        # sem chaves: falha rapida de roteamento, sem rede, sem onda
        with self.assertRaises(proxy_mod.SemChaveDisponivel) as ctx:
            proxy_mod._abrir(Estado(), cfg, corpo)
        self.assertFalse(str(ctx.exception).startswith("onda:"),
                         f"pedido pequeno nao passa pela onda: {ctx.exception}")


class TestFronteiraReal(unittest.TestCase):
    def test_estaciona_e_completa_apos_virada(self):
        # UNICO teste lento: alinha a fronteira real :00 e prova que o segundo
        # grande estaciona e so completa no minuto seguinte.
        from app.estado import Estado
        from app.onda import WaveScheduler, bucket_id, next_wave_ts
        limite_s = 65
        t0 = time.time()
        while time.time() % 60 <= 50:
            if time.time() - t0 > limite_s:
                self.skipTest("nao alineou a fronteira :00 a tempo")
            time.sleep(0.2)
        inicio = time.time()
        balde0 = bucket_id(inicio)
        estado = Estado()
        cfg = _cfg_base(onda_limite_tokens=10,
                        onda_max_na_fila=48, onda_max_espera_seg=20,
                        onda_jitter_max_ms=0, onda_adianto_ms=0)
        sched = WaveScheduler(estado, cfg)
        self.assertTrue(sched.reivindicar("G1"))  # ocupa o minuto atual
        self.assertTrue(sched.minuto_ocupado("G1"))
        self.assertFalse(sched.reivindicar("G1"), "lease ja ocupado recusa 2o claim")
        espera = sched.tempo_para_proxima_onda()
        self.assertGreater(espera, 0)
        # dorme como o proxy: fatias <=2s
        restante = float(espera)
        while restante > 0:
            fatia = min(2.0, restante)
            time.sleep(fatia)
            restante -= fatia
        fim = time.time()
        balde1 = bucket_id(fim)
        self.assertNotEqual(balde1, balde0, "deveria ter cruzado a fronteira :00")
        self.assertGreaterEqual(fim - inicio, (60 - (inicio % 60)) - 1.0)
        # minuto seguinte libera o lease antigo e aceita novo disparo
        self.assertFalse(sched.minuto_ocupado("G1"))
        self.assertTrue(sched.reivindicar("G1"))
        self.assertTrue(sched.minuto_ocupado("G1"))
        self.assertGreaterEqual(sched.depth(), 1)


if __name__ == "__main__":
    unittest.main()
