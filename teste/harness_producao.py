#!/usr/bin/env python3
"""Harness de teste de PRODUCAO do gateway Monitor-Google.

Dispara uma conversa real multi-turno contra http://127.0.0.1:<porta>/v1,
passando pelo roteador e pelas chaves reais do Google.

Regras:
  - NUNCA usa modelo contendo "3.7" (modelo principal de producao).
  - Preferencia: gemini-3.5-flash-lite -> gemini-3.5-flash -> gemini-3.6-flash.
  - Testa: /v1/models, chat nao-streaming com memoria de contexto (matematica),
    chat streaming SSE, registro das requisicoes em /api/requisicoes.

Uso:
    python teste\\harness_producao.py
    python teste\\harness_producao.py --turnos 5 --host 127.0.0.1 --porta 8011
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent

MODELO_PROIBIDO = "3.7"
PREFERENCIA = ["gemini-3.5-flash-lite", "gemini-3.5-flash", "gemini-3.6-flash"]

resultados = []


def registrar(nome, ok, detalhe=""):
    resultados.append((nome, ok, detalhe))
    marca = "PASS" if ok else "FAIL"
    print(f"  [{marca}] {nome}" + (f" - {detalhe}" if detalhe else ""))


class Gateway:
    def __init__(self, host, porta, token):
        self.base = f"http://{host}:{porta}"
        self.token = (token or "").strip()

    def _headers(self, extra=None):
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if extra:
            h.update(extra)
        return h

    def get(self, caminho, timeout=20):
        req = urllib.request.Request(self.base + caminho, headers=self._headers())
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())

    def chat(self, mensagens, modelo, stream=False, timeout=150):
        corpo = {
            "model": modelo,
            "messages": mensagens,
            "max_tokens": 1200,
            "reasoning_effort": "low",
            "stream": stream,
        }
        dados = json.dumps(corpo).encode("utf-8")
        req = urllib.request.Request(
            self.base + "/v1/chat/completions",
            data=dados,
            headers=self._headers(),
            method="POST",
        )
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as erro:
            return erro.code, erro.read().decode("utf-8", "ignore"), None
        if stream:
            return resp.status, None, resp
        return resp.status, json.loads(resp.read()), None


def extrair_conteudo(resposta):
    try:
        return resposta["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return ""


def extrair_numero(texto):
    digitos = "".join(c for c in texto if c.isdigit())
    return int(digitos) if digitos else None


def escolher_modelo(gw):
    status, dados = gw.get("/v1/models")
    if status != 200:
        return [], f"/v1/models falhou com HTTP {status}"
    ids = [m.get("id") for m in dados.get("data") or [] if m.get("id")]
    if not ids:
        return [], "/v1/models retornou lista vazia"
    proibidos = [i for i in ids if MODELO_PROIBIDO in i]
    candidatos = [m for m in PREFERENCIA if m in ids]
    if not candidatos:
        candidatos = [
            m for m in ids if MODELO_PROIBIDO not in m and "preview" not in m and "latest" not in m
        ]
    return candidatos, f"{len(ids)} modelos expostos; proibidos({MODELO_PROIBIDO}) presentes: {proibidos or 'nenhum'}"


def conversa_nao_streaming(gw, modelo):
    historico = []
    esperado_seq = [("60", "minutos em uma hora"), ("120", "dobro do numero")]
    ok_geral = True
    latencias = []

    perguntas = [
        ("Responda APENAS com o numero, sem texto: quantos minutos tem uma hora?", 60),
        ("Multiplique o numero da minha mensagem anterior por 2. Responda APENAS com o numero.", 120),
        ("Sem recalcular: qual foi o primeiro numero que voce me respondeu nesta conversa? Responda APENAS com ele.", 60),
        ("Some os dois ultimos numeros desta conversa e responda APENAS com o resultado.", 180),
    ]

    for i, (pergunta, esperado) in enumerate(perguntas, 1):
        historico.append({"role": "user", "content": pergunta})
        inicio = time.time()
        status, resposta, _ = gw.chat(list(historico), modelo)
        ms = int((time.time() - inicio) * 1000)
        latencias.append(ms)
        if status != 200:
            registrar(f"T{i} nao-streaming", False, f"HTTP {status}: {resposta[:160]}")
            ok_geral = False
            break
        conteudo = extrair_conteudo(resposta)
        historico.append({"role": "assistant", "content": conteudo})
        numero = extrair_numero(conteudo)
        acertou = numero == esperado
        ok_geral = ok_geral and acertou
        uso = resposta.get("usage") or {}
        registrar(
            f"T{i} memoria/matematica (esperado {esperado})",
            acertou,
            f"'{conteudo.strip()[:60]}' | {ms}ms | tokens in/out={uso.get('prompt_tokens')}/{uso.get('completion_tokens')}",
        )

    media = sum(latencias) // len(latencias) if latencias else 0
    registrar("Conversa multi-turno coerente", ok_geral, f"latencia media {media}ms")
    return ok_geral, latencias


def conversa_streaming(gw, modelo):
    mensagens = [{"role": "user", "content": "Conte de 1 ate 5 separado por espaco. Responda so isso."}]
    inicio = time.time()
    status, _, resp = gw.chat(mensagens, modelo, stream=True)
    if status != 200:
        corpo = ""
        try:
            corpo = resp.read().decode("utf-8", "ignore")[:160]
        except Exception:
            pass
        registrar("Streaming SSE abriu", False, f"HTTP {status}: {corpo}")
        return False

    partes = []
    tin = tout = 0
    primeiro_chunk_ms = None
    try:
        for linha in resp:
            texto = linha.decode("utf-8", "ignore").strip()
            if not texto.startswith("data:"):
                continue
            carga = texto[5:].strip()
            if carga == "[DONE]":
                break
            try:
                dados = json.loads(carga)
            except ValueError:
                continue
            if primeiro_chunk_ms is None:
                primeiro_chunk_ms = int((time.time() - inicio) * 1000)
            escolhas = dados.get("choices") or [{}]
            delta = (escolhas[0] or {}).get("delta") or {}
            if delta.get("content"):
                partes.append(delta["content"])
            uso = dados.get("usage")
            if uso:
                tin = uso.get("prompt_tokens") or tin
                tout = uso.get("completion_tokens") or tout
    finally:
        try:
            resp.close()
        except OSError:
            pass

    total = "".join(partes)
    tem_1_a_5 = all(str(n) in total for n in range(1, 6))
    registrar(
        "Streaming SSE completo (1..5)",
        tem_1_a_5 and bool(tin or tout),
        f"'{total.strip()[:60]}' | TTFB {primeiro_chunk_ms}ms | usage in/out={tin}/{tout}",
    )
    return tem_1_a_5


def verificar_registro(gw, minimos):
    status, dados = gw.get("/api/requisicoes?limite=50")
    if status != 200:
        registrar("Registro em /api/requisicoes", False, f"HTTP {status}")
        return
    reqs = dados.get("requisicoes") or []
    recentes_ok = [r for r in reqs if r.get("ok") == 1]
    registradas = len(recentes_ok)
    if registradas >= min(2, minimos):
        ultima = recentes_ok[0]
        registrar(
            "Monitor registrou as chamadas",
            True,
            f"{registradas} ok recentes; ultima chave={ultima.get('chave')} tokens_in={ultima.get('tokens_in')}",
        )
    else:
        registrar(
            "Monitor registrou as chamadas",
            False,
            f"so {registradas} registros ok (suspeita: banco somente-leitura, ver RELATORIO_AUDITORIA.md item A1)",
        )


def main():
    parser = argparse.ArgumentParser(description="Teste de producao do gateway (nunca usa 3.7)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--porta", type=int, default=None)
    args = parser.parse_args()

    cfg_path = RAIZ / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    porta = args.porta or int(cfg.get("porta", 8011))
    gw = Gateway(args.host, porta, cfg.get("gateway_token"))

    print(f"== Harness de producao -> {gw.base} ==")

    try:
        status, saude = gw.get("/api/saude")
        registrar("Health /api/saude", status == 200 and saude.get("ok") is True, str(saude))
    except Exception as erro:
        registrar("Health /api/saude", False, str(erro))
        imprimir_resumo()
        return 1

    candidatos, detalhe = escolher_modelo(gw)
    registrar("/v1/models sem 3.7 selecionavel", bool(candidatos), detalhe)
    if not candidatos:
        imprimir_resumo()
        return 1

    modelo_usado = None
    latencias = []
    for candidato in candidatos:
        print(f"\n-- Conversa real (nao-streaming) com {candidato} --")
        ok, lat = conversa_nao_streaming(gw, candidato)
        latencias.extend(lat)
        if ok:
            modelo_usado = candidato
            break
        print(f"(modelo {candidato} falhou; tentando proximo candidato)")

    if modelo_usado is None and candidatos:
        modelo_usado = candidatos[0]

    print(f"\n-- Conversa real (streaming) com {modelo_usado} --")
    conversa_streaming(gw, modelo_usado)

    print("\n-- Verificacao do monitor local --")
    verificar_registro(gw, minimos=len(resultados))

    imprimir_resumo()
    falhas = sum(1 for _, ok, _ in resultados if not ok)
    return 1 if falhas else 0


def imprimir_resumo():
    print("\n== RESUMO ==")
    for nome, ok, detalhe in resultados:
        print(f"  [{'PASS' if ok else 'FAIL'}] {nome}" + (f" - {detalhe}" if detalhe else ""))
    total = len(resultados)
    passou = sum(1 for _, ok, _ in resultados if ok)
    print(f"\n{passou}/{total} verificacoes passaram")


if __name__ == "__main__":
    sys.exit(main())
