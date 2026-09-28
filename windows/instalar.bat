@echo off
setlocal
cd /d "%~dp0.."
echo === Instalacion del bot de musica ===
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] No se encontro Python. Instalalo desde https://www.python.org/downloads/
    echo         y marca la casilla "Add python.exe to PATH" durante la instalacion.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creando entorno virtual .venv ...
    python -m venv .venv || (echo [ERROR] No se pudo crear el entorno virtual & pause & exit /b 1)
)

echo Instalando dependencias...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -U -r requirements.txt || (echo [ERROR] Fallo pip install & pause & exit /b 1)

where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo.
    echo FFmpeg no esta instalado. Intentando instalarlo con winget...
    winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
    echo Si winget fallo, descarga FFmpeg de https://www.gyan.dev/ffmpeg/builds/
    echo y pon la ruta de ffmpeg.exe en FFMPEG_PATH dentro del archivo .env
)

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo.
    echo Se creo el archivo .env - abrelo y pega el token de tu bot en DISCORD_TOKEN.
)

rem Guarda la ruta completa de ffmpeg en .env (la tarea programada puede no ver el PATH actualizado)
for /f "delims=" %%F in ('where ffmpeg 2^>nul') do (
    powershell -NoProfile -Command "(Get-Content '.env' -Encoding UTF8) -replace '^FFMPEG_PATH=.*$', 'FFMPEG_PATH=%%F' | Set-Content '.env' -Encoding UTF8"
    goto :ffmpeg_done
)
:ffmpeg_done

echo.
echo === Listo ===
echo 1. Edita .env y pon tu DISCORD_TOKEN
echo 2. Prueba el bot con windows\probar_bot.bat
echo 3. Cuando funcione, ejecuta windows\registrar_tarea.bat para que arranque con el PC
echo.
echo (Si winget acaba de instalar FFmpeg, cierra esta ventana y vuelve a ejecutar
echo  instalar.bat para que se guarde su ruta en .env)
pause
