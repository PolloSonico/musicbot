@echo off
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] No se encontro el entorno de Python del bot ^(.venv^).
    echo         Ejecuta primero windows\instalar.bat y revisa que termine sin errores.
    echo         Carpeta actual: %cd%
    pause
    exit /b 1
)
if not exist ".env" (
    echo [ERROR] Falta el archivo .env. Ejecuta windows\instalar.bat y pon tu token en .env
    pause
    exit /b 1
)
echo Iniciando el bot en modo prueba (cierra la ventana o pulsa Ctrl+C para detenerlo)...
".venv\Scripts\python.exe" bot.py
pause
