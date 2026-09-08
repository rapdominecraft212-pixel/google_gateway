import json
import os
from pathlib import Path

from . import PASTA


def caminho_config():
    return Path(os.environ.get("MONITOR_CONFIG") or PASTA / "config.json")


def caminho_db():
    return Path(os.environ.get("MONITOR_DB") or PASTA / "historico.db")


CAMPOS_LIMITE = (
    "limite_rpm",
    "limite_tpm",
    "limite_requisicoes_dia",
    "limite_tokens_dia",
    "limite_tokens_mes",
)

PADRAO = {
    "porta": 8787,
    "intervalo_uso_seg": 60,
    "limiar_alerta": 80,
    "max_conc_por_key": 2,
    "cooldown_429_seg": 15,
    "cooldown_429_max_seg": 300,
    "cooldown_net_seg": 30,
    "cooldown_5xx_seg": 10,
    "max_ciclos_espera": 3,
    "espera_cooldown_max_seg": 30,
    "timeout_http_seg": 120,
    "timeout_episodio_seg": 30,
    "episodio_janela_seg": 300,
    "episodio_min_503": 2,
    "episodio_min_chaves": 2,
    "racers_cap_total": 6,
    "racers_janela": 10,
    "chaves_por_tentativa": 3,
    "max_espera_requisicao_seg": 120,
    "tentativas_rede": 8,
    "tentativas_rede_seg": 45,
    "reasoning_effort": "medium",
    "max_tokens_padrao": 32768,
    "onda_limite_tokens": 200000,
    "onda_max_na_fila": 48,
    "onda_max_espera_seg": 65,
    "onda_jitter_max_ms": 2000,
    "onda_adianto_ms": 250,
    "gateway_token": "",
    # Cota compartilhada por PROJETO (true quando todas as keys pertencem ao
    # mesmo projeto Google - caso do AI Studio, que cria um projeto unico).
    # Limites oficiais sao por projeto, nao por chave (RELATORIO_GEMINI_API
    # 8.1). Com true: UM balde de RPM/TPM para a frota, valvula global,
    # admissao preditiva pelo grupo e cooldown de grupo no 429 de quota.
    "cota_compartilhada": False,
    "chaves": [],
    "modelos_indisponiveis": [],
    "limites": {
        "rpm": 20,
        "tpm": 250000,
        "requisicoes_dia": 1500,
        "tokens_dia": 0,
        "tokens_mes": 20000000,
    },
    "modelos": [
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-tts-preview",
    ],
}


def carregar():
    cfg = dict(PADRAO)
    caminho = caminho_config()
    if caminho.exists():
        try:
            dados = json.loads(caminho.read_text(encoding="utf-8"))
        except ValueError as erro:
            raise RuntimeError(f"config.json invalido: {erro}")
        if not isinstance(dados, dict):
            raise RuntimeError("config.json precisa ser um objeto JSON")
        cfg.update(dados)
    return cfg


def salvar(cfg):
    caminho = caminho_config()
    caminho.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def chaves_validas(cfg):
    out = []
    nomes = set()
    for item in cfg.get("chaves") or []:
        if not isinstance(item, dict):
            continue
        key = (item.get("key") or "").strip()
        if not key or "COLE-SUA-KEY" in key:
            continue
        nome = (item.get("nome") or "").strip()
        base = nome or f"key-{len(out) + 1}"
        final = base
        n = 2
        while final in nomes:
            final = f"{base}-{n}"
            n += 1
        nomes.add(final)
        entrada = {
            "nome": final,
            "key": key,
            "tipo": (item.get("tipo") or "gemini").lower(),
            "ativa": item.get("ativa") is not False,
        }
        for campo in CAMPOS_LIMITE:
            if item.get(campo) is not None:
                entrada[campo] = item[campo]
        out.append(entrada)
    return out


def chaves_ativas(cfg):
    return [c for c in chaves_validas(cfg) if c["ativa"]]


def tentativas_por_pedido(cfg):
    """Teto de tentativas de um pedido: TODAS as chaves da pool (uma passada
    completa antes de admitir que nao havia saida).

    A pool e controlada externamente pelo config.json (a UI tambem mexe nela
    por /api/chaves): adicionar ou remover chaves ajusta o teto automaticamente
    no pedido seguinte, sem tocar no codigo e sem reiniciar. Nao existe
    override por config: um teto menor que a pool deixava chaves sadias na
    gaveta e devolvia erro ao usuario (foi o que 'tentativas_por_pedido: 5'
    fazia com 36 chaves)."""
    return max(1, len(chaves_ativas(cfg)))


def mascarar(key):
    if not key or len(key) <= 10:
        return key
    return f"{key[:6]}...{key[-4:]}"
