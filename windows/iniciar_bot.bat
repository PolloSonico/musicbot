@echo off
rem Lo ejecuta la tarea programada. Actualiza yt-dlp y mantiene el bot vivo:
rem si el bot se cierra o se cae, lo vuelve a lanzar a los 15 segundos.
setlocal
cd /d "%~dp0.."
if not exist logs mkdir logs

echo [%date% %time%] Actualizando yt-dlp y librerias de IA... >> logs\launcher.log
".venv\Scripts\python.exe" -m pip install -U --quiet --disable-pip-version-check "yt-dlp[default,deno]" google-genai >> logs\launcher.log 2>&1

:loop

echo [%date% %time%] Iniciando bot >> logs\launcher.log
".venv\Scripts\python.exe" "%cd%\bot.py" >> logs\launcher.log 2>&1
echo [%date% %time%] El bot se cerro (codigo %errorlevel%). Reiniciando en 15 s... >> logs\launcher.log

rem "timeout" no funciona sin consola (tarea programada), por eso usamos ping como espera
ping -n 16 127.0.0.1 >nul
goto loop
