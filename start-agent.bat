@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem  jobauto -- run the local agent right now.
rem
rem  This is the "before Task Scheduler starts it" launcher. It mirrors the
rem  setup-agent.bat flow: it ensures the local venv exists, installs the
rem  package from this repo if needed, checks the agent is linked, and then
rem  runs the jobauto agent loop in the foreground.

title jobauto -- run agent

set "SRC=%~dp0"
set "HOME_DIR=%USERPROFILE%\.jobauto"
set "VENV=%HOME_DIR%\venv"
set "PY=%VENV%\Scripts\python.exe"
set "EXE=%VENV%\Scripts\jobauto.exe"

echo.
echo   jobauto -- running agent manually
echo   ------------------------------------------------------------
echo.

rem ----------------------------------------------------------- python
if not exist "%PY%" (
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
)

rem ------------------------------------------------------------ install
if not exist "%EXE%" (
    echo   [1/3] installing jobauto from this folder ...
    "%PY%" -m pip install --quiet --upgrade pip
    "%PY%" -m pip install --quiet --upgrade "%SRC%."
    if errorlevel 1 (
        echo.
        echo   [X] The install failed. Run this to see why:
        echo         "%PY%" -m pip install --upgrade "%SRC%."
        goto :finish_fail
    )
)

if not exist "%EXE%" (
    echo.
    echo   [X] Installed, but %EXE% is missing.
    echo       That usually means the install went to a different environment.
    goto :finish_fail
)

rem --------------------------------------------------------------- linked?
if not exist "%HOME_DIR%\data\agent.json" (
    echo.
    echo   [!] This PC is not linked to your dashboard yet.
    echo.
    echo       Open your site, go to Devices, copy the token, then run:
    echo         "%EXE%" link --url=https://YOUR-SITE.onrender.com --token=YOUR_TOKEN
    echo.
    echo       Linking sets up automatic running by itself.
    goto :finish_ok
)

rem ------------------------------------------------------------- run agent
echo   [2/3] starting the agent in the foreground ...
echo.
"%EXE%" agent
if errorlevel 1 (
    echo.
    echo   [X] The agent stopped with an error.
    echo       Try this for details:
    echo         "%EXE%" agent
    goto :finish_fail
)

goto :finish_ok

:finish_ok
echo.
rem Leave the window open while the agent is running; it is the foreground
rem process that keeps the browser session alive.
pause
endlocal
exit /b 0

:finish_fail
echo.
pause
endlocal
exit /b 1
