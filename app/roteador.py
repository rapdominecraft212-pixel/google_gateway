import time

LIMITE_ESGOTADA = 99.5


def _elegiveis(estado, cfg, chaves, max_conc, agora, extra_cheia=None, tokens_necessarios=0):
    out = []
    for chave in chaves:
        nome = chave["nome"]
        if chave.get("ativa") is False:
            continue
        if nome in estado.invalidas:
            continue
        if estado.cooldown_ate.get(nome, 0) > agora:
            continue
        if extra_cheia is not None and extra_cheia(nome):
            continue
        if tokens_necessarios > 0:
            # Admissao preditiva de TPM: a folga tem que caber o pedido INTEIRO,
            # contando o que ja esta em voo na chave (reservado). Sem isso, dois
            # pedidos grandes concorrentes entram na mesma chave e o 2o leva 429.
            # Excecao: pedido MAIOR que o teto cabe UMA vez em balde pristino
            # (Google aprova 1 oversized por balde vazio); sem isso, oversized
            # seria recusado por todas as chaves = deadlock de admissao.
            livres = estado.tpm_disponivel(nome)
            if livres is not None and livres < tokens_necessarios:
                try:
                    teto = estado.teto_de(nome)
                except Exception:
                    teto = 0
                if not (teto > 0 and tokens_necessarios > teto
                        and estado.balde_pristino(nome)):
                    continue
        percent = estado.percent_ativo(nome)
        if percent is not None and percent >= LIMITE_ESGOTADA:
            continue
        if estado.em_voo.get(nome, 0) >= max_conc:
            continue
        out.append(chave)
    return out


def escolher(estado, cfg, agora=None, extra_cheia=None, tokens_necessarios=0):
    candidatas, razao = escolher_n(
        estado, cfg, 1, agora, extra_cheia, tokens_necessarios=tokens_necessarios
    )
    if not candidatas:
        return None, razao
    return candidatas[0], None


def escolher_n(estado, cfg, n, agora=None, extra_cheia=None, tokens_necessarios=0):
    agora = agora or time.time()
    chaves = cfg.get("chaves")
    if not chaves:
        return [], "nenhuma chave configurada"

    por_nome = {c["nome"]: c for c in chaves if c.get("nome")}

    def elegivel(nome):
        return bool(_elegiveis(estado, cfg, [por_nome[nome]], int(cfg.get("max_conc_por_key", 2)),
                               agora, extra_cheia, tokens_necessarios))

    # MODELO DA CAIXA: um unico "balcao" global compartilhado. Todo agente
    # (pedido) pega SEMPRE da FRENTE da fila; a chave usada vai para o FUNDO.
    # Agentes paralelos recebem chaves distintas sem coordenacao: a fila e a
    # memoria compartilhada. Sem rotacao aqui = sonda apenas.
    nomes = estado.pegar_chaves(
        list(por_nome.keys()), max(1, int(n or 1)), elegivel, rotacionar=False
    )
    if not nomes:
        return [], "todas as chaves estao cheias, em cooldown ou invalidas"
    return [por_nome[nome] for nome in nomes], None
