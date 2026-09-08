from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

FUSO_PACIFICO = ZoneInfo("America/Los_Angeles")

JANELAS = ("minuto", "dia", "mes")

CAMPOS = {
    "limite_rpm": "rpm",
    "limite_tpm": "tpm",
    "limite_requisicoes_dia": "requisicoes_dia",
    "limite_tokens_dia": "tokens_dia",
    "limite_tokens_mes": "tokens_mes",
}


def limites_por_chave(cfg, chave):
    limites = dict(cfg.get("limites") or {})
    for campo, alvo in CAMPOS.items():
        if chave.get(campo) is not None:
            limites[alvo] = chave[campo]
    return limites


def _local_aware(momento):
    return momento if momento.tzinfo is not None else momento.astimezone()


def _pacifico(momento):
    return _local_aware(momento).astimezone(FUSO_PACIFICO)


def _iso_utc(momento):
    return momento.astimezone(timezone.utc).isoformat(timespec="seconds")


def _iso_local(momento):
    return momento.astimezone().isoformat(timespec="seconds")


def desde_dia_iso(agora=None):
    agora = _local_aware(agora or datetime.now())
    la = _pacifico(agora)
    meia_noite = la.replace(hour=0, minute=0, second=0, microsecond=0)
    return _iso_local(meia_noite)


def desde_mes_iso(agora=None):
    agora = _local_aware(agora or datetime.now())
    la = _pacifico(agora)
    primeiro = la.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return _iso_local(primeiro)


def _proxima_meia_noite_pacifico(agora):
    la = _pacifico(agora)
    meia_noite = la.replace(hour=0, minute=0, second=0, microsecond=0)
    return meia_noite + timedelta(days=1)


def reset_dia_ts(agora=None):
    return _proxima_meia_noite_pacifico(agora or datetime.now()).timestamp()


def reset_mes_ts(agora=None):
    return _proximo_primeiro_pacifico(agora or datetime.now()).timestamp()


def _proximo_primeiro_pacifico(agora):
    la = _pacifico(agora)
    primeiro = la.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if primeiro.month == 12:
        return primeiro.replace(year=primeiro.year + 1, month=1)
    return primeiro.replace(month=primeiro.month + 1)


def _reset_minuto(primeiro_iso):
    if not primeiro_iso:
        return None
    try:
        inicio = _local_aware(datetime.fromisoformat(primeiro_iso))
    except ValueError:
        return None
    return _iso_utc(inicio + timedelta(seconds=60))


def _percent(*pares):
    valores = []
    for valor, limite in pares:
        try:
            limite = float(limite)
        except (TypeError, ValueError):
            continue
        if limite > 0:
            valores.append(float(valor or 0) / limite * 100.0)
    return max(valores) if valores else None


def calcular(uso, limites, agora=None):
    agora = agora or datetime.now()
    minuto = (uso or {}).get("minuto") or {}
    dia = (uso or {}).get("dia") or {}
    mes = (uso or {}).get("mes") or {}
    return {
        "minuto": {
            "percent": _percent(
                (minuto.get("reqs"), limites.get("rpm")),
                (minuto.get("tokens"), limites.get("tpm")),
            ),
            "resetsAt": _reset_minuto(minuto.get("primeiro")),
        },
        "dia": {
            "percent": _percent(
                (dia.get("reqs"), limites.get("requisicoes_dia")),
                (dia.get("tokens"), limites.get("tokens_dia")),
            ),
            "resetsAt": _iso_utc(_proxima_meia_noite_pacifico(agora)),
        },
        "mes": {
            "percent": _percent((mes.get("tokens"), limites.get("tokens_mes"))),
            "resetsAt": _iso_utc(_proximo_primeiro_pacifico(agora)),
        },
    }
