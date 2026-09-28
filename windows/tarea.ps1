# Gestiona la tarea programada que arranca el bot al encender el PC.
# Uso: tarea.ps1 -Accion registrar | quitar | reiniciar | detener
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("registrar", "quitar", "reiniciar", "detener")]
    [string]$Accion
)

$TaskName = "MusicBot Discord"
$Root = Split-Path -Parent $PSScriptRoot
$Launcher = Join-Path $PSScriptRoot "iniciar_bot.bat"

# Pedir permisos de administrador si hace falta
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Start-Process powershell.exe -Verb RunAs -ArgumentList @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"", "-Accion", $Accion)
    exit
}

function Stop-Bot {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    }
    # Por si quedaron procesos del bot sueltos
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe' OR Name='cmd.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*$Root*" -and
                       ($_.CommandLine -like "*bot.py*" -or $_.CommandLine -like "*\iniciar_bot.bat*") } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

switch ($Accion) {
    "registrar" {
        if (-not (Test-Path (Join-Path $Root ".venv\Scripts\python.exe"))) {
            Write-Host "Primero ejecuta windows\instalar.bat" -ForegroundColor Red
            Read-Host "Pulsa Enter para salir"; exit 1
        }
        if (-not (Test-Path (Join-Path $Root ".env"))) {
            Write-Host "Falta el archivo .env con tu DISCORD_TOKEN (ejecuta instalar.bat)" -ForegroundColor Red
            Read-Host "Pulsa Enter para salir"; exit 1
        }

        $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$Launcher`"" -WorkingDirectory $Root
        $trigger = New-ScheduledTaskTrigger -AtStartup
        $trigger.Delay = "PT30S"   # espera 30 s a que haya red
        # S4U: corre con tu usuario, sin iniciar sesion, sin ventana y sin guardar tu contrasena
        $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
            -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
            -MultipleInstances IgnoreNew

        Stop-Bot
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
            -Principal $principal -Settings $settings -Force `
            -Description "Bot de musica de Discord ($Root)" | Out-Null
        Start-ScheduledTask -TaskName $TaskName

        Write-Host "Tarea '$TaskName' registrada y en marcha." -ForegroundColor Green
        Write-Host "El bot arrancara solo cada vez que enciendas el PC (no hace falta iniciar sesion)."
        Write-Host "Logs: $Root\logs"
    }
    "quitar" {
        Stop-Bot
        if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        }
        Write-Host "Tarea eliminada. El bot ya no arrancara con el PC." -ForegroundColor Yellow
    }
    "reiniciar" {
        Stop-Bot
        Start-ScheduledTask -TaskName $TaskName
        Write-Host "Bot reiniciado." -ForegroundColor Green
    }
    "detener" {
        Stop-Bot
        Write-Host "Bot detenido (volvera a arrancar en el proximo encendido)." -ForegroundColor Yellow
    }
}
Read-Host "Pulsa Enter para cerrar"
