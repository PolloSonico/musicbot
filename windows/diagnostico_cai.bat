@echo off
cd /d "%~dp0.."
echo Probando la conexion con Character.AI...
".venv\Scripts\python.exe" cai_diagnostico.py
echo.
echo Copia todo lo de arriba y pasaselo a Claude.
pause
