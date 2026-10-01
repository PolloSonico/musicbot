@echo off
rem Corre las pruebas automaticas del bot (no tocan tus datos ni se conectan a internet).
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r requirements-dev.txt
".venv\Scripts\python.exe" -m pytest tests -q
echo.
if errorlevel 1 (echo [!] Alguna prueba fallo: copia lo de arriba y pasaselo a Claude.) else (echo Todo bien.)
pause
