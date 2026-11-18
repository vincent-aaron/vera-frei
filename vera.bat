@echo off
rem Start Vera in this window. Other commands: vera.bat find, stats, prefs, rounds, answer
rem This runs the OLD standalone bot. Normally Vera runs inside Hermes: use vera-on.bat / vera-off.bat. Never run both.
cd /d "%~dp0"
call .venv\Scripts\activate.bat
if not "%~1"=="" (
    python -m vera %*
    goto :eof
)
rem Keep Vera online: if she stops unexpectedly (crash, network drop), restart after 10 seconds.
rem Close this window to stop her for good.
:loop
echo [%date% %time%] starting Vera...
python -m vera
if "%errorlevel%"=="3" goto :eof
echo [%date% %time%] Vera stopped (exit code %errorlevel%). Restarting in 10 seconds... >> data\vera.log
echo Vera stopped (exit code %errorlevel%). Restarting in 10 seconds - close this window to stop.
timeout /t 10 /nobreak >nul
goto loop
