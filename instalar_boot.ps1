# instalar_boot.ps1 — fecha o buraco do "garantia de boot".
#
# Hoje o launcher só nasce de um .lnk na pasta Startup, que dispara apenas
# em LOGIN INTERATIVO: se a máquina liga e ninguém faz login, o gemini não
# sobe. Este script registra a tarefa de agendamento "MonitorGemini-Boot",
# que roda `pythonw.exe launcher.py --boot` no STARTUP DO SISTEMA, com ou
# sem usuário logado.
#
# O launcher se autodeduplica (launcher.pid + checagem da porta 8011), então
# a tarefa de boot e o .lnk de Startup podem coexistir sem corrida: quem
# chegar primeiro supervisiona, o outro apenas sai.
#
# Uso:
#   powershell -NoProfile -ExecutionPolicy Bypass -File instalar_boot.ps1              # instala
#   powershell -NoProfile -ExecutionPolicy Bypass -File instalar_boot.ps1 -Uninstall   # remove
#
# Executar em PowerShell ELEVADO (Register-ScheduledTask com principal SYSTEM
# exige privilégios de administrador).
#
# Por que SYSTEM e não "usuário atual com highest privileges": rodar "se o
# usuário estiver logado ou não" como usuário comum exige senha armazenada
# (-Password, ou -S4U que quebra acesso a recursos de rede/perfil em alguns
# cenários). SYSTEM não pede senha, dispara antes de qualquer login e o
# launcher usa caminhos absolutos — nada depende de perfil de usuário.

[CmdletBinding()]
param(
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

$TaskName = 'MonitorGemini-Boot'
$Repo     = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }

function Write-Cmdlets {
    Write-Host ''
    Write-Host 'Cmdlets PowerShell usados nesta operação (equivalente schtasks em comentário):' -ForegroundColor Cyan
    Write-Host '  Get-ScheduledTask / Unregister-ScheduledTask   (schtasks /Query, schtasks /Delete /TN <nome> /F)'
    Write-Host '  New-ScheduledTaskAction                        (schtasks /Create ... /TR)'
    Write-Host '  New-ScheduledTaskTrigger -AtStartup            (schtasks /Create ... /SC ONSTART)'
    Write-Host '  New-ScheduledTaskSettingsSet                   (schtasks /Create ... /RL /RI /RU)'
    Write-Host '  New-ScheduledTaskPrincipal -UserId SYSTEM      (schtasks /Create ... /RU SYSTEM /RL HIGHEST)'
    Write-Host '  Register-ScheduledTask                         (schtasks /Create /TN <nome> /XML ...)'
}

if ($Uninstall) {
    Write-Host "Removendo a tarefa '$TaskName'..." -ForegroundColor Yellow
    Write-Cmdlets
    $existente = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existente) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Tarefa '$TaskName' removida." -ForegroundColor Green
    } else {
        Write-Host "Tarefa '$TaskName' nao estava registrada; nada a fazer." -ForegroundColor Gray
    }
    return
}

# --- instala ---------------------------------------------------------------
$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue)
if (-not $pythonw) {
    throw "pythonw.exe nao encontrado no PATH. Instale o Python ou ajuste este script."
}
$launcher = Join-Path $Repo 'launcher.py'
if (-not (Test-Path -LiteralPath $launcher)) {
    throw "launcher.py nao encontrado em '$Repo'. Rode este script de dentro do repositorio."
}

Write-Host "Registrando tarefa '$TaskName'..." -ForegroundColor Yellow
Write-Host "  executar  : $($pythonw.Source) launcher.py --boot"
Write-Host "  diretorio : $Repo"
Write-Host "  gatilho   : AtStartup (inicializacao do sistema, sem precisar de login)"
Write-Cmdlets

$action = New-ScheduledTaskAction -Execute $pythonw.Source `
    -Argument 'launcher.py --boot' `
    -WorkingDirectory $Repo

# ONSTART: dispara na inicializacao do SO, usuario logado ou nao.
$trigger = New-ScheduledTaskTrigger -AtStartup

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -MultipleInstances IgnoreNew

# SYSTEM + highest: sem senha, antes de qualquer login, sem desktop (o
# launcher detecta isso e marca launcher_start motivo=boot pelo --boot).
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description 'Monitor-Google: supervisiona o gateway gemini (porta 8011) desde o boot, sem depender de login interativo.'

Get-ScheduledTask -TaskName $TaskName | Format-List TaskName, State
Write-Host "Pronto. Para testar sem reiniciar: Start-ScheduledTask -TaskName '$TaskName'" -ForegroundColor Green
Write-Host "Para reverter: powershell -File instalar_boot.ps1 -Uninstall" -ForegroundColor Green
