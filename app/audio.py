"""TTS via gateway: POST /v1/audio/speech (padrao OpenAI) -> Gemini nativo.

Mantem o comportamento de chat intacto: so acrescenta audio, com a mesma
rotacao de chaves (failover sequencial, 1 cota por tentativa), cooldown e
registro no banco.
"""

import base64
import io
import time
import urllib.error
import wave

from . import config, db, gemini_api
from .janelas import reset_dia_ts, reset_mes_ts
from .roteador import escolher_n

# Vozes OpenAI -> vozes Gemini (se o cliente mandar voz Gemini, passa direto).
MAPA_VOZES = {
    "alloy": "Kore",
    "echo": "Puck",
    "fable": "Charon",
    "onyx": "Fenrir",
    "nova": "Aoede",
    "shimmer": "Zephyr",
}


def normalizar_voz(voz):
    nome = (voz or "").strip() or "Kore"
    baixo = nome.lower()
    if baixo in MAPA_VOZES:
        return MAPA_VOZES[baixo]
    # "kore" -> "Kore" (API e case-sensitive); se ja for nome valido, preserva.
    if len(nome) <= 32 and nome.replace("-", "").replace("_", "").isalpha():
        return nome[:1].upper() + nome[1:]
    return nome


def e_modelo_tts(modelo):
    return "tts" in (modelo or "").lower()


def pcm_para_wav(pcm: bytes, taxa=24000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(taxa)
        w.writeframes(pcm)
    return buf.getvalue()


def _taxa_do_mime(mime):
    # "audio/L16;rate=24000" -> 24000
    try:
        for parte in (mime or "").replace(";", " ").split():
            if parte.startswith("rate="):
                taxa = int(parte.split("=", 1)[1])
                if 8000 <= taxa <= 48000:
                    return taxa
    except (ValueError, TypeError):
        pass
    return 24000


def converter(audio_b64, mime, formato):
    bruto = base64.b64decode(audio_b64)
    fmt = (formato or "wav").lower()
    if fmt == "pcm":
        return bruto, "audio/pcm"
    if fmt in ("wav", "x-wav"):
        # Gemini ja devolve PCM 16-bit; embrulha em WAV (stdlib, sem ffmpeg).
        if bruto[:4] == b"RIFF":
            return bruto, "audio/wav"
        return pcm_para_wav(bruto, _taxa_do_mime(mime)), "audio/wav"
    raise ValueError(f"formato '{formato}' nao suportado (use wav ou pcm)")


class SemChaveDisponivel(Exception):
    def __init__(self, mensagem, retry_after=None):
        super().__init__(mensagem)
        self.retry_after = retry_after


def gerar(estado, cfg, modelo, texto, voz="Kore", formato="wav"):
    """Tenta as chaves em sequencia (1 cota por tentativa, sem corrida paralela)."""
    if not texto or not texto.strip():
        raise ValueError("campo 'input' vazio")
    if len(texto) > 8000:
        raise ValueError("texto longo demais para TTS (max ~8000 caracteres)")
    timeout = int(cfg.get("timeout_http_seg", 120))
    cooldown_base = int(cfg.get("cooldown_429_seg", 15))
    teto_429 = int(cfg.get("cooldown_429_max_seg", 300))
    cooldown_5xx = int(cfg.get("cooldown_5xx_seg", 10))
    tentativas_max = config.tentativas_por_pedido(cfg)
    deadline = time.time() + int(cfg.get("max_espera_requisicao_seg", 120))
    voz_gemini = normalizar_voz(voz)
    candidatas, razao = escolher_n(estado, cfg, tentativas_max)
    if not candidatas:
        raise SemChaveDisponivel(razao or "sem chave disponivel", 60)
    motivos = []
    usadas = set()
    for chave in candidatas[:tentativas_max]:
        if time.time() > deadline:
            break
        nome = chave["nome"]
        if nome in usadas:
            continue
        usadas.add(nome)
        estado.incrementar_em_voo(nome)
        # MODELO DA CAIXA: chave disparada vai para o fundo da fila global.
        try:
            estado.rotacionar_fila(nome)
        except Exception:
            pass
        inicio = time.time()
        try:
            mime, audio_b64 = gemini_api.gerar_fala(
                chave["key"], modelo, texto, voz_gemini, timeout=timeout
            )
            audio, content_type = converter(audio_b64, mime, formato)
        except ValueError as erro:
            # Erro local (formato invalido, resposta sem audio): nao queima a chave.
            estado.decrementar_em_voo(nome)
            raise
        except urllib.error.HTTPError as erro:
            estado.decrementar_em_voo(nome)
            try:
                corpo_erro = erro.read()
            except OSError:
                corpo_erro = b""
            desc = gemini_api.descricao_erro(erro.code, corpo_erro)
            ms = int((time.time() - inicio) * 1000)
            if erro.code in (400, 404, 416, 422):
                db.registrar_requisicao(nome, modelo, False, status=erro.code, ms=ms, erro=desc)
                # 400 em TTS quase sempre e parametro (voz/modelo/texto): repassa
                # sem queimar as demais chaves.
                raise SemChaveDisponivel(desc, None) from erro
            db.registrar_requisicao(nome, modelo, False, status=erro.code, ms=ms, erro=desc)
            if erro.code in (401, 403):
                estado.marcar_invalida(nome)
            elif erro.code == 429:
                alvo = gemini_api.quota_reset_alvo(corpo_erro)
                if alvo == "mes":
                    estado.marcar_quota(nome, "cota mensal esgotada: " + desc, reset_mes_ts())
                elif alvo == "dia":
                    estado.marcar_quota(nome, "cota diaria esgotada: " + desc, reset_dia_ts())
                else:
                    retry = gemini_api.retry_delay_seg(corpo_erro) or cooldown_base
                    estado.marcar_retry(nome, "rate limit (TTS): " + desc, time.time() + min(retry, teto_429))
            elif erro.code in (408,) or erro.code >= 500:
                estado.marcar_retry(nome, f"indisponivel (HTTP {erro.code}): {desc}", time.time() + cooldown_5xx)
            else:
                estado.marcar_erro(nome, desc)
            motivos.append(f"{nome}: {desc}")
            continue
        except (urllib.error.URLError, OSError) as erro:
            estado.decrementar_em_voo(nome)
            ms = int((time.time() - inicio) * 1000)
            db.registrar_requisicao(nome, modelo, False, ms=ms, erro=str(erro))
            estado.marcar_erro(nome, str(erro))
            motivos.append(f"{nome}: {erro}")
            continue
        ms = int((time.time() - inicio) * 1000)
        try:
            # TTS nao devolve usage de tokens; registra 1 req ok com os chars
            # no erro vazio para aparecer no painel como audio.
            db.registrar_requisicao(nome, modelo, True, status=200, ms=ms,
                                    tokens_in=len(texto), tokens_out=0, erro=None)
        except Exception:
            pass
        estado.marcar_sucesso(nome)
        estado.registrar_uso_local(nome, 1)
        estado.decrementar_em_voo(nome)
        return audio, content_type, nome
    retry = estado.menor_retry_restante() or estado.menor_quota_reset() or 60
    raise SemChaveDisponivel("; ".join(motivos) if motivos else "sem chave disponivel", int(retry) + 2)
