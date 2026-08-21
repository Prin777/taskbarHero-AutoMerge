param(
    [string]$PythonExe = "",
    [switch]$ValidateOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectRoot

if ($ValidateOnly) {
    Write-Host "build.ps1 syntax and startup validation passed."
    exit 0
}

$venvDirectory = Join-Path $projectRoot ".venv-build"
$venvPython = Join-Path $venvDirectory "Scripts\python.exe"
$recreateEnvironment = -not (Test-Path -LiteralPath $venvPython)
if (-not $recreateEnvironment) {
    & $venvPython --version
    $recreateEnvironment = $LASTEXITCODE -ne 0
}

if ($recreateEnvironment) {
    if ([string]::IsNullOrWhiteSpace($PythonExe)) {
        foreach ($commandName in @("python.exe", "py.exe")) {
            $pythonCommand = Get-Command $commandName -ErrorAction SilentlyContinue
            if ($null -ne $pythonCommand) {
                & $pythonCommand.Source --version
                if ($LASTEXITCODE -eq 0) {
                    $PythonExe = $pythonCommand.Source
                    break
                }
            }
        }
        if ([string]::IsNullOrWhiteSpace($PythonExe)) {
            throw "Python 3.11 or newer was not found. Install Python or pass -PythonExe with its full path."
        }
    } else {
        & $PythonExe --version
        if ($LASTEXITCODE -ne 0) {
            throw "The selected Python executable cannot start: $PythonExe"
        }
    }

    if (Test-Path -LiteralPath $venvDirectory) {
        $resolvedProject = [System.IO.Path]::GetFullPath($projectRoot).TrimEnd('\')
        $resolvedEnvironment = [System.IO.Path]::GetFullPath($venvDirectory)
        if (-not $resolvedEnvironment.StartsWith($resolvedProject + '\', [System.StringComparison]::OrdinalIgnoreCase) -or
            [System.IO.Path]::GetFileName($resolvedEnvironment) -ne ".venv-build") {
            throw "Refusing to remove an unexpected build environment path: $resolvedEnvironment"
        }
        Write-Host "Existing build environment is invalid; recreating it."
        Remove-Item -LiteralPath $resolvedEnvironment -Recurse -Force
    }
    & $PythonExe -m venv $venvDirectory
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create the build environment."
    }
}

& $venvPython -m pip install --disable-pip-version-check --upgrade pyinstaller
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install PyInstaller."
}

& $venvPython -m PyInstaller --noconfirm --clean --onefile --windowed --name "TBH-AutoSynth" "taskbar_hero.py"
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed."
}

$outputPath = Join-Path $projectRoot "dist\TBH-AutoSynth.exe"
Write-Host "Build completed: $outputPath"
