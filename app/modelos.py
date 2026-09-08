from . import config, gemini_api

EXTENSOES_IGNORAR = (
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

# TTS ("-tts") e propositalmente permitido: o gateway serve texto em
# /v1/chat/completions (como antes) E audio em /v1/audio/speech.
# "live"/"audio" continuam bloqueados (WebSocket, nao REST simples).


def e_tts(mid):
    return "tts" in (mid or "").lower()


def para_chat(modelos):
    """Sublista so com modelos de texto (sem TTS) para o opencode.json."""
    return [m for m in (modelos or []) if isinstance(m, str) and m and not e_tts(m)]

# Modelos que AINDA aparecem na listagem da API, mas retornam 404 no chat
# ("no longer available to new users"). Mantidos fora do gateway pra nao
# ressuscitar no proximo sync. Atualizar quando o Google mudar o aviso.
MODELOS_DEPRECADOS = {
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.5-flash-lite",
}


def _util_para_chat(mid, mortos=frozenset()):
    nome = mid.lower()
    if nome.startswith("tuned"):
        return False
    if nome in MODELOS_DEPRECADOS or mid in mortos or nome in {m.lower() for m in mortos}:
        return False
    return not any(marca in nome for marca in EXTENSOES_IGNORAR)


def sincronizar(cfg, salvar=True):
    for chave in cfg.get("chaves") or []:
        if chave.get("ativa") is False:
            continue
        try:
            ids = gemini_api.listar_modelos(chave["key"])
        except Exception:
            ids = None
        if not ids:
            continue
        ids = [m for m in ids if _util_para_chat(m, set(cfg.get("modelos_indisponiveis") or []))]
        if not ids:
            continue
        atuais = [m for m in (cfg.get("modelos") or []) if isinstance(m, str) and m]
        mantidos = [m for m in atuais if m in ids]
        novos = [m for m in ids if m not in atuais]
        atualizado = cfg["modelos"] != mantidos + novos
        cfg["modelos"] = mantidos + novos
        if atualizado and salvar:
            config.salvar(cfg)
        return {"ok": True, "fonte": chave["nome"], "total": len(cfg["modelos"])}
    return {"ok": False, "motivo": "nenhuma chave ativa conseguiu listar modelos"}