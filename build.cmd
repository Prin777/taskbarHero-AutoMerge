@echo off
setlocal EnableExtensions DisableDelayedExpansion

for %%I in ("%~dp0.") do set "PROJECT_ROOT=%%~fI"
set "VENV_DIR=%PROJECT_ROOT%\.venv-build"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"
set "ICON_PATH=%PROJECT_ROOT%\assets\taskbar-hero.ico"
set "PENDING_ROOT=%PROJECT_ROOT%\build\package-pending"
set "PENDING_EXE=%PENDING_ROOT%\dist\TBH-AutoSynth.exe"
set "FINAL_DIR=%PROJECT_ROOT%\dist"
set "FINAL_EXE=%FINAL_DIR%\TBH-AutoSynth.exe"

pushd "%PROJECT_ROOT%" || exit /b 1

if not exist "%ICON_PATH%" (
    echo [ERROR] Application icon not found: "%ICON_PATH%"
    goto :fail
)

if exist "%VENV_PYTHON%" (
    "%VENV_PYTHON%" -c "from PyInstaller.utils.hooks.tcl_tk import tcltk_info; assert tcltk_info.available" >nul 2>&1
    if not errorlevel 1 goto :environment_ready
    echo Existing build environment cannot package Tcl/Tk and must be recreated.
)

set "BASE_PYTHON="
if not "%~1"=="" set "BASE_PYTHON=%~f1"
if not defined BASE_PYTHON (
    for /f "usebackq delims=" %%P in (`py.exe -3 -c "import sys; print(sys.executable)" 2^>nul`) do if not defined BASE_PYTHON set "BASE_PYTHON=%%P"
)
if not defined BASE_PYTHON (
    for /f "usebackq delims=" %%P in (`where.exe python.exe 2^>nul`) do if not defined BASE_PYTHON set "BASE_PYTHON=%%P"
)
if not defined BASE_PYTHON (
    echo [ERROR] Python 3.11 or newer with Tkinter was not found.
    echo Run: build.cmd "C:\full\path\to\python.exe"
    goto :fail
)

"%BASE_PYTHON%" -c "import sys, tkinter; assert sys.version_info >= (3, 11)" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Selected Python must be version 3.11 or newer and include Tkinter:
    echo "%BASE_PYTHON%"
    goto :fail
)

for %%I in ("%PROJECT_ROOT%\.venv-build") do set "EXPECTED_VENV=%%~fI"
for %%I in ("%VENV_DIR%") do set "RESOLVED_VENV=%%~fI"
if /i not "%RESOLVED_VENV%"=="%EXPECTED_VENV%" (
    echo [ERROR] Refusing to replace unexpected build environment: "%RESOLVED_VENV%"
    goto :fail
)
if exist "%RESOLVED_VENV%" rmdir /s /q "%RESOLVED_VENV%"
"%BASE_PYTHON%" -m venv "%VENV_DIR%"
if errorlevel 1 goto :fail

:environment_ready
"%VENV_PYTHON%" -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    "%VENV_PYTHON%" -m pip install --disable-pip-version-check pyinstaller
    if errorlevel 1 goto :fail
)
"%VENV_PYTHON%" -c "import PIL, pystray" >nul 2>&1
if errorlevel 1 (
    "%VENV_PYTHON%" -m pip install --disable-pip-version-check -r requirements.txt
    if errorlevel 1 goto :fail
)

"%VENV_PYTHON%" -m PyInstaller --noconfirm --clean --onefile --windowed --name "TBH-AutoSynth" --icon "%ICON_PATH%" --add-data "%ICON_PATH%;assets" --distpath "%PENDING_ROOT%\dist" --workpath "%PENDING_ROOT%\work" --specpath "%PENDING_ROOT%\spec" "taskbar_hero.py"
if errorlevel 1 goto :fail
if not exist "%PENDING_EXE%" (
    echo [ERROR] PyInstaller did not produce the expected executable.
    goto :fail
)
findstr /c:"missing module named tkinter" "%PENDING_ROOT%\work\TBH-AutoSynth\warn-TBH-AutoSynth.txt" >nul 2>&1
if not errorlevel 1 (
    echo [ERROR] The build environment cannot package Tkinter. The previous EXE was kept.
    echo Rebuild with a complete Python installation that includes Tcl/Tk.
    goto :fail
)

set "TBH_ICON_PATH=%ICON_PATH%"
set "TBH_EXE_PATH=%PENDING_EXE%"
powershell.exe -NoProfile -Command "Add-Type -AssemblyName System.Drawing; $source=[System.Drawing.Icon]::new($env:TBH_ICON_PATH,32,32); $target=[System.Drawing.Icon]::ExtractAssociatedIcon($env:TBH_EXE_PATH); $a=$source.ToBitmap(); $b=$target.ToBitmap(); $different=0; for($y=0;$y -lt 32;$y++){for($x=0;$x -lt 32;$x++){if($a.GetPixel($x,$y).ToArgb() -ne $b.GetPixel($x,$y).ToArgb()){$different++}}}; $a.Dispose(); $b.Dispose(); $source.Dispose(); $target.Dispose(); if($different -ne 0){Write-Error ('Embedded icon mismatch: '+$different+' pixels differ'); exit 1}"
if errorlevel 1 (
    echo [ERROR] Packaged EXE icon verification failed; the previous EXE was kept.
    goto :fail
)

if not exist "%FINAL_DIR%" mkdir "%FINAL_DIR%"
if exist "%FINAL_EXE%" del /f /q "%FINAL_EXE%"
move /y "%PENDING_EXE%" "%FINAL_EXE%" >nul
if errorlevel 1 goto :fail

echo Build completed and icon verified:
echo "%FINAL_EXE%"
popd
exit /b 0

:fail
echo Build failed. The previous dist\TBH-AutoSynth.exe was not replaced.
popd
exit /b 1
