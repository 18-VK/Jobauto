@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem  jobauto -- set this PC up to run on its own.
rem
rem  Double-click it, or run it from a terminal. Safe to run again at any time:
rem  every step is idempotent, and it upgrades an existing install in place
rem  rather than replacing it, so your portal logins, your database and your
rem  link to the dashboard all survive.
rem
rem  Deliberately NOT an editable install. An editable install makes the
rem  package resolve to this folder, and jobauto then treats itself as a source
rem  checkout and keeps its data beside the code -- which moves the agent away
rem  from the ~\.jobauto\data that holds your existing link and logins.

title jobauto -- set up automatic running

set "SRC=%~dp0"
set "HOME_DIR=%USERPROFILE%\.jobauto"
set "VENV=%HOME_DIR%\venv"
set "PY=%VENV%\Scripts\python.exe"
set "EXE=%VENV%\Scripts\jobauto.exe"

echo.
echo   jobauto -- setting this PC to run by itself
echo   ------------------------------------------------------------
echo.

rem ----------------------------------------------------------- python
if exist "%PY%" goto :have_venv

echo   No environment yet. Creating one in %HOME_DIR% ...
set "SYSPY="
for %%C in (python.exe py.exe python3.exe) do (
    if not defined SYSPY (
        for /f "delims=" %%P in ('where %%C 2^>nul') do (
            if not defined SYSPY set "SYSPY=%%P"
        )
    )
)

if not defined SYSPY (
    echo.
    echo   [X] Python was not found on this PC.
    echo.
    echo       Install Python 3.10 or newer, ticking "Add python.exe to PATH":
    echo         https://www.python.org/downloads/
    echo       Or run:  winget install Python.Python.3.12
    echo.
    goto :finish_fail
)

echo   using !SYSPY!
"!SYSPY!" -m venv "%VENV%"
if errorlevel 1 (
    echo.
    echo   [X] Could not create the environment in %VENV%.
    goto :finish_fail
)

:have_venv
echo   [1/4] environment ready

rem ------------------------------------------------------------ install
rem  --upgrade so re-running this picks up new code. Quiet, because pip's
rem  normal output buries the one line that matters if something fails.
echo   [2/4] installing jobauto from this folder ^(a minute or two^) ...
"%PY%" -m pip install --quiet --upgrade pip
"%PY%" -m pip install --quiet --upgrade "%SRC%."
if errorlevel 1 (
    echo.
    echo   [X] The install failed. Run this to see why:
    echo         "%PY%" -m pip install --upgrade "%SRC%."
    goto :finish_fail
)

if not exist "%EXE%" (
    echo.
    echo   [X] Installed, but %EXE% is missing.
    echo       That usually means the install went to a different environment.
    goto :finish_fail
)

rem  Fast when the browser is already there, so it costs nothing to re-check.
echo   [3/4] checking the browser ...
"%PY%" -m playwright install chromium >nul 2>&1
if errorlevel 1 (
    echo         could not download it -- portal work will fail until it is there:
    echo           "%PY%" -m playwright install chromium
)

rem --------------------------------------------------------------- linked?
rem  Autostart on an unlinked PC registers a task that exits immediately every
rem  time. Better to stop here and say so than to leave that running.
if not exist "%HOME_DIR%\data\agent.json" (
    echo.
    echo   [!] This PC is not linked to your dashboard yet.
    echo.
    echo       Open your site, go to Devices, copy the token, then run:
    echo         "%EXE%" link --url=https://YOUR-SITE.onrender.com --token=YOUR_TOKEN
    echo.
    echo       Linking sets up automatic running by itself, so you will not
    echo       need this file again afterwards.
    goto :finish_ok
)

rem ------------------------------------------------------------- autostart
echo   [4/4] registering it to run on its own ...
echo.
"%EXE%" autostart
if errorlevel 1 (
    echo.
    echo   The task scheduler refused. Trying the Startup folder instead --
    echo   weaker: it starts at logon only, with no restart if it stops.
    "%EXE%" autostart --startup-folder
    if errorlevel 1 (
        echo.
        echo   [X] Automatic running could not be set up on this PC.
        echo       Start it by hand when you need it:
        echo         "%EXE%" agent
        goto :finish_fail
    )
)

echo.
echo   ------------------------------------------------------------
"%EXE%" autostart --status
echo   ------------------------------------------------------------
echo.
echo   Done. You can close this window -- the agent keeps running,
echo   starts again when you log in, and restarts itself if it stops.
echo.

:finish_ok
echo.
pause
endlocal
exit /b 0

:finish_fail
echo.
pause
endlocal
exit /b 1
