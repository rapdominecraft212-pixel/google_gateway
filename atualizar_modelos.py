"""Atualiza a lista de modelos do gateway com o que o Google oferece agora.

Uso:
    python atualizar_modelos.py
    python atualizar_modelos.py --opencode C:\\Users\\User\\.config\\opencode\\opencode.json

Sem --opencode, so atualiza config.json e o exemplo opencode.json.example.
Com --opencode, mescla (ou cria) o bloco "gemini-gateway" no seu opencode.json
sem tocar nos outros providers.
"""

import argparse
import json
import sys
from pathlib import Path

from app import config, gemini_api

PREFIXOS_IGNORAR = ("embedding", "imagen", "veo", "aqa", "tts", "music")

# Modelos que ainda aparecem na listagem da API, mas retornam 404 no chat
# ("no longer available to new users").
MODELOS_DEPRECADOS = {
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.5-flash-lite",
}


def _util_para_gateway(mid):
    # TTS ("tts") e permitido de proposito: serve /v1/audio/speech.
    # "live"/"audio" continuam fora (WebSocket, nao REST simples).
    nome = mid.lower()
    if nome.startswith("tuned"):
        return False
    if nome in MODELOS_DEPRECADOS:
        return False
    return not any(
        marca in nome
        for marca in (
            "embedding",
            "imagen",
            "veo",
            "aqa",
            "music",
            "live",
            "audio",
            "robotics",
            "computer-use",
            "lyria",
            "banana",
            "deep-research",
            "antigravity",
            "customtools",
            "-image",
        )
    )


def _util_para_chat(mid):
    return _util_para_gateway(mid) and "tts" not in mid.lower()


def _nome_bonito(mid):
    restante = mid.split("-", 1)[1] if "-" in mid else mid
    partes = [p[:1].upper() + p[1:].lower() for p in restante.split("-")]
    return "Gemini " + " ".join(partes)


def bloco_provider(modelos, porta=8011):
    # opencode so usa chat: TTS fica fora daqui (continua no config.json
    # do gateway para /v1/audio/speech e /v1/models).
    so_chat = [m for m in (modelos or []) if _util_para_chat(m)]
    return {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            "gemini-gateway": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Gemini Gateway (multi-key)",
                "options": {
                    "baseURL": f"http://127.0.0.1:{porta}/v1",
                    "apiKey": "gateway-local",
                },
                "models": {m: {"name": _nome_bonito(m)} for m in so_chat},
            }
        },
    }


def _primeira_chave_ativa(cfg):
    for chave in config.chaves_ativas(cfg):
        return chave
    return None


def principal():
    args = argparse.ArgumentParser(description="Sincroniza modelos do Google no gateway")
    args.add_argument("--opencode", help="caminho do opencode.json para mesclar o provider")
    opcoes = args.parse_args()

    cfg = config.carregar()
    chave = _primeira_chave_ativa(cfg)
    if chave is None:
        print("Nenhuma chave valida no config.json. Cole suas keys em config.json e rode de novo.")
        sys.exit(1)

    ids = gemini_api.listar_modelos(chave["key"])
    if not ids:
        print(f"NAO consegui listar modelos com a chave {chave['nome']} (sem conexao ou chave invalida).")
        sys.exit(1)

    ids = [m for m in ids if _util_para_gateway(m)]
    atuais = [m for m in (cfg.get("modelos") or []) if isinstance(m, str) and m]
    mantidos = [m for m in atuais if m in ids]
    novos = [m for m in ids if m not in atuais]
    cfg["modelos"] = mantidos + novos
    config.salvar(cfg)
    print(f"config.json: {len(cfg['modelos'])} modelos")
    exemplo = Path(__file__).parent / "opencode.json.example"
    exemplo.write_text(
        json.dumps(bloco_provider(cfg["modelos"], porta=int(cfg.get("porta", 8011))), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("opencode.json.example: atualizado")

    if opcoes.opencode:
        caminho = Path(opcoes.opencode)
        if caminho.exists():
            dados = json.loads(caminho.read_text(encoding="utf-8"))
        else:
            dados = {}
        dados.setdefault("provider", {})["gemini-gateway"] = bloco_provider(
            cfg["modelos"], porta=int(cfg.get("porta", 8011))
        )["provider"]["gemini-gateway"]
        caminho.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{caminho}: bloco gemini-gateway mesclado (modelos atualizados)")

    print("\nModelos:")
    for m in cfg["modelos"]:
        print(f"  - {m}")


if __name__ == "__main__":
    principal()