@echo off
setlocal
title Monitor-Google - Teste de cota real
cd /d "%~dp0"

rem ==================================================================
rem  MONITOR-GOOGLE / google_gateway - TESTE DE COTA REAL (Windows)
rem  Fluxo: GitHub Desktop - aceita o PR - abre a pasta local -
rem         2 cliques neste arquivo. Leia docs\DIAGNOSTICO_COTA.md (secao 6)
rem  Resultado: arquivo novo em teste\resultados\ (pronto para commit)
rem ==================================================================

chcp 65001 >nul 2>nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

echo ==================================================================
echo  MONITOR-GOOGLE - TESTE DE COTA REAL
echo  Confirmacao do bug de cota compartilhada (docs\DIAGNOSTICO_COTA.md)
echo ==================================================================
echo.

rem ---------- 1) achar o Python ----------
set "PYPATH="
set "PYARGS="

python -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYPATH=python"

if not defined PYPATH (
    py -3 -c "import sys" >nul 2>nul
    if not errorlevel 1 (
        set "PYPATH=py"
        set "PYARGS=-3"
    )
)

if not defined PYPATH if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PYPATH=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PYPATH if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYPATH=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PYPATH if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PYPATH=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"

if not defined PYPATH (
    echo ERRO: Python nao foi encontrado neste computador.
    echo        Instale em https://www.python.org/downloads/ e marque a opcao
    echo        "Add python.exe to PATH" na instalacao. Depois rode este .bat de novo.
    echo.
    pause
    exit /b 1
)

echo Python encontrado: %PYPATH% %PYARGS%
echo.

rem ---------- 2) config.json (fica fora do git de proposito) ----------
if exist config.json goto config_ok
echo config.json NAO existe nesta pasta (ele fica fora do git para proteger
echo suas keys). Procurando o config.json do Monitor-Google original...
echo.
if exist "%USERPROFILE%\Desktop\Monitor-Google\config.json" (
    copy /y "%USERPROFILE%\Desktop\Monitor-Google\config.json" "config.json" >nul
    echo Achado no Desktop - Monitor-Google original. Copiado pra ca.
    goto config_ok
)
if exist "%USERPROFILE%\OneDrive\Desktop\Monitor-Google\config.json" (
    copy /y "%USERPROFILE%\OneDrive\Desktop\Monitor-Google\config.json" "config.json" >nul
    echo Achado no Desktop OneDrive - Monitor-Google original. Copiado pra ca.
    goto config_ok
)
echo Nao achei automaticamente. Voce pode:
echo   1) copiar manualmente o config.json do seu Monitor-Google para esta pasta, ou
echo   2) digitar o caminho completo dele agora.
echo.
set "CFGPATH="
set /p "CFGPATH=Caminho do config.json (ou ENTER para sair): "
if "%CFGPATH%"=="" (
    echo Nada feito. Coloque o config.json nesta pasta e rode este .bat de novo.
    pause
    exit /b 1
)
if not exist "%CFGPATH%" (
    echo Esse caminho nao existe: %CFGPATH%
    pause
    exit /b 1
)
copy /y "%CFGPATH%" "config.json" >nul
echo Copiado.
echo.

:config_ok
echo.

rem ---------- 3) rodar a coleta ----------
echo Tudo pronto. O teste faz ~76 pedidos reais ao Google (~500 tokens,
echo 3 a 10 minutos). Nao feche esta janela.
echo.
"%PYPATH%" %PYARGS% "teste\coletar_dados_cota.py"
set "RC=%ERRORLEVEL%"

echo.
echo ==================================================================
if "%RC%"=="0" (
    echo TERMINOU OK.
) else (
    echo TERMINOU COM CODIGO %RC% - um parcial pode ter sido salvo mesmo assim.
)
echo.
echo PROXIMO PASSO - GitHub Desktop:
echo   1. Abra o GitHub Desktop: vai aparecer arquivo novo em teste\resultados\
echo   2. Escreva o commit (ex: "resultado do teste de cota") e clique em Commit
echo   3. Clique em "Push origin" para subir
echo   Depois do push o agente remoto le o resultado e segue as correcoes.
echo ==================================================================
echo.
pause
