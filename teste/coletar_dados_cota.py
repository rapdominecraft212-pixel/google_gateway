#!/usr/bin/env python3
"""Coleta o pacote completo de dados do teste de cota real (fluxo GitHub Desktop).

Cenario de uso (ver docs/DIAGNOSTICO_COTA.md, secao 6):
  1. O usuario aceita o PR no GitHub Desktop e abre a pasta local do repo.
  2. Duplo clique em RODAR-TESTE-COTA.bat (na raiz do repo).
  3. O .bat garante Python + config.json e chama ESTE script, que:
       - grava um cabecalho de ambiente (maquina, Python, branch/commit do
         git, config SANITIZADO - as keys NUNCA sao gravadas - e aviso se o
         gateway local estiver no ar, pois trafego paralelo contaminaria a
         medicao);
       - roda o teste REAL teste/teste_cota_real.py --yes-real capturando
         TODA a saida (stdout+stderr) ao vivo, linha a linha, em
         teste/resultados/cota-real-<data>-<hora>.txt;
       - grava rodape com custo e instrucoes de commit/push.
  4. O usuario commita e pusha o arquivo de resultados pelo GitHub Desktop.
  5. O agente remoto le o resultado e segue o plano F1-F6.

Zero mock: este script nao altera nem simula componente nenhum - so
orquestra e captura. O teste de baixo e o teste real de producao de sempre.
Se for interrompido (Ctrl+C), o parcial ja gravado e preservado.

Modos:
  (sem flag)   teste real completo (~76 pedidos reais, ~500 tokens, 3-10 min)
  --ensaio     so a canalizacao: roda SEM --yes-real (o teste recusa e sai
               com custo zero) - para validar o fluxo sem gastar cota
  --force      pula o aviso de "resultado recente" (< 30 min)
"""
import json
import os
import platform
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

TESTE = RAIZ / "teste" / "teste_cota_real.py"
PASTA_RESULTADOS = RAIZ / "teste" / "resultados"
LINHA = "=" * 78

ENV_PROXY = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
             "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy")


def _git(*args):
    """Saida de um comando git local (best-effort; GitHub Desktop nao poe
    git no PATH, entao falhar aqui e normal e nao e erro)."""
    try:
        out = subprocess.run(["git", *args], cwd=str(RAIZ), capture_output=True,
                             text=True, timeout=10, errors="replace")
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


def _gateway_no_ar(porta):
    """True se algo estiver escutando na porta do gateway neste momento."""
    try:
        with socket.create_connection(("127.0.0.1", int(porta)), timeout=1.5):
            return True
    except OSError:
        return False


def _config_sanitizado():
    """Resumo do config.json SEM nenhuma key. O arquivo de resultados vai
    para o git: segredo nenhum pode vazar por aqui."""
    from app import config as config_mod  # import so depois de garantir config.json
    cfg = config_mod.carregar()
    chaves = cfg.get("chaves") or []
    ativas = sum(1 for c in chaves if c.get("ativa") is not False)
    resumo = {
        "n_chaves": len(chaves),
        "n_ativas": ativas,
        "porta": cfg.get("porta"),
        "host": cfg.get("host"),
        "limites": cfg.get("limites"),
        "n_modelos": len(cfg.get("modelos") or []),
        "modelos": cfg.get("modelos") or [],
        "gateway_token_definido": bool((cfg.get("gateway_token") or "").strip()),
        "chaves (nome/ativa, key oculta)": [
            {"nome": c.get("nome"), "ativa": c.get("ativa") is not False}
            for c in chaves
        ],
    }
    return resumo


def _cabecalho(modo):
    linhas = [LINHA,
              "PACOTE DE DADOS - TESTE DE COTA REAL",
              "Monitor-Google / google_gateway (ver docs/DIAGNOSTICO_COTA.md)",
              LINHA,
              f"Gerado em        : {datetime.now().astimezone().isoformat(timespec='seconds')}",
              f"Maquina          : {os.environ.get('COMPUTERNAME', '?')} / "
              f"{os.environ.get('USERNAME', '?')} | {platform.platform()}",
              f"Python           : {platform.python_version()} ({sys.executable})",
              f"Pasta do repo    : {RAIZ}",
              f"Branch           : {_git('rev-parse', '--abbrev-ref', 'HEAD') or '(git indisponivel)'}",
              f"Commit           : {(_git('rev-parse', '--short', 'HEAD') or '(git indisponivel)')}"]
    env_base = os.environ.get("GEMINI_API_BASE")
    linhas.append(f"GEMINI_API_BASE  : {env_base or '(nao definido - vai para o Google real)'}"
                  + ("  << ATENCAO: endpoint sobrescrito!" if env_base else ""))
    proxies = [v for v in ENV_PROXY if os.environ.get(v)]
    linhas.append(f"Proxies de rede  : {', '.join(proxies) if proxies else '(nenhum)'}")

    try:
        resumo = _config_sanitizado()
        linhas.append(f"Config sanitizado: {resumo['n_chaves']} chaves "
                      f"({resumo['n_ativas']} ativas) | porta {resumo['porta']} | "
                      f"{resumo['n_modelos']} modelos | limites: {json.dumps(resumo['limites'])}")
        linhas.append("                   (keys OCULTAS de proposito - elas nao vao para o git)")
        if resumo["n_ativas"] == 0:
            linhas.append("                   << ERRO: nenhuma chave ativa no config.json!")
        if _gateway_no_ar(resumo["porta"] or 0):
            linhas.append(f"Gateway local    : ESTA NO AR (porta {resumo['porta']}) << AVISO: "
                          "trafego paralelo do gateway/opencode pode contaminar a medicao;")
            linhas.append("                   o ideal e parar o launcher e fechar o opencode antes do teste.")
        else:
            linhas.append(f"Gateway local    : nao esta no ar (porta {resumo['porta']}) - medicao limpa")
    except Exception as erro:
        linhas.append(f"Config sanitizado: FALHOU ({erro})")
    linhas += [f"Modo             : {modo}",
               LINHA,
               ">>> SAIDA COMPLETA DE teste/teste_cota_real.py " + ("--yes-real" if "REAL" in modo else "(sem --yes-real)"),
               LINHA]
    return linhas


def _rodape(arquivo, rc, duracao, interrompido, custo):
    msg_interrompido = ("\nINTERROMPIDO com Ctrl+C - o parcial acima foi preservado "
                        "e ja serve de dados.") if interrompido else ""
    return [
        LINHA,
        f"<<< FIM DA SAIDA (codigo {rc}, {duracao:.0f}s){msg_interrompido}",
        LINHA,
        f"CUSTO APROXIMADO : {custo}",
        f"ARQUIVO          : {arquivo.relative_to(RAIZ)}",
        LINHA,
        "PROXIMO PASSO (GitHub Desktop):",
        "  1. Abra o GitHub Desktop - vai aparecer arquivo novo em teste\\resultados\\",
        '  2. Escreva o commit (ex.: "resultado do teste de cota") e clique em "Commit to ..."',
        '  3. Clique em "Push origin" para subir.',
        "Depois do push, o agente remoto le este arquivo e segue o plano F1-F6",
        "(docs/DIAGNOSTICO_COTA.md, secao 7).",
        LINHA,
    ]


def main(argv):
    ensaio = "--ensaio" in argv
    forcar = "--force" in argv

    try:  # console do Windows: saida UTF-8 sem quebrar
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print(LINHA)
    print("COLETA DE DADOS - TESTE DE COTA REAL")
    print(LINHA)

    # guarda: nao gastar cota a toa com dupla execucao
    PASTA_RESULTADOS.mkdir(parents=True, exist_ok=True)
    anteriores = sorted(PASTA_RESULTADOS.glob("cota-real-*.txt"))
    if anteriores and not forcar and not ensaio:
        idade_min = (time.time() - anteriores[-1].stat().st_mtime) / 60.0
        if idade_min < 30:
            print(f"AVISO: ja existe resultado de {idade_min:.0f} min atras "
                  f"({anteriores[-1].name}).")
            try:
                resp = input("Cada execucao custa ~76 pedidos reais (~500 tokens). "
                             "Rodar mesmo assim? [s/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                resp = ""
            if resp not in ("s", "sim", "y", "yes"):
                print("Cancelado. Nada foi gasto.")
                return 0

    if ensaio:
        print("MODO ENSAIO: valida a canalizacao com CUSTO ZERO (o teste vai")
        print("recusar rodar sem --yes-real e sair - isso e o esperado).")
    else:
        print("Este teste faz ~76 pedidos REAIS ao Google (~500 tokens, 3 a 10 min).")
        print("Nao feche esta janela. Ctrl+C interrompe e o parcial fica salvo.")
        try:
            input("\nENTER para comecar (Ctrl+C para cancelar)... ")
        except (EOFError, KeyboardInterrupt):
            print("\nCancelado antes de comecar. Nada foi gasto.")
            return 0

    modo = "REAL (--yes-real)" if not ensaio else "ENSAIO (custo zero)"
    ts = time.strftime("%Y%m%d-%H%M%S")
    arquivo = PASTA_RESULTADOS / f"cota-real-{ts}.txt"

    argv_teste = [sys.executable, str(TESTE)] + ([] if ensaio else ["--yes-real"])
    cab = _cabecalho(modo)

    rc, interrompido, t0 = None, False, time.time()
    with open(arquivo, "w", encoding="utf-8") as f:
        for linha in cab:
            f.write(linha + "\n")
        f.flush()
        for linha in cab:
            print(linha)
        try:
            proc = subprocess.Popen(argv_teste, cwd=str(RAIZ),
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT,
                                    text=True, errors="replace", bufsize=1)
            for linha in proc.stdout:
                sys.stdout.write(linha)
                sys.stdout.flush()
                f.write(linha)
                f.flush()
            rc = proc.wait()
        except KeyboardInterrupt:
            interrompido = True
            try:
                proc.kill()
            except Exception:
                pass
            try:
                rc = proc.wait()
            except Exception:
                rc = -1
            print("\n[coletor] interrompido (Ctrl+C) - salvando o parcial...")
            f.write("\n[coletor] interrompido (Ctrl+C) pelo usuario.\n")

        duracao = time.time() - t0
        custo = ("~zero (ensaio)" if ensaio else
                 "ver linha CUSTO TOTAL acima (o proprio teste reporta)")
        rodape = _rodape(arquivo, rc if rc is not None else -1, duracao,
                         interrompido, custo)
        for linha in rodape:
            f.write(linha + "\n")
        f.flush()
        for linha in rodape:
            print(linha)

    print(f"\n[coletor] arquivo gravado: {arquivo}")
    return 0 if not interrompido else 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
