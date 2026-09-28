@echo off
cd /d "%~dp0.."
echo Iniciando el bot en modo prueba (cierra la ventana o pulsa Ctrl+C para detenerlo)...
".venv\Scripts\python.exe" bot.py
pause
