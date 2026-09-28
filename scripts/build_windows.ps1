[CmdletBinding()]
param(
    [string]$Python = "python",
    [switch]$SkipTests,
    [string]$OutputRoot = "dist"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
    throw "This build script only supports Windows."
}

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$specPath = Join-Path $projectRoot "scripts\12306FairTicket.spec"
$distRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $OutputRoot))
if (-not $distRoot.StartsWith($projectRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "OutputRoot must be a directory inside this checkout."
}
$distDir = Join-Path $distRoot "12306FairTicket"
$workRoot = Join-Path $projectRoot ("build\" + (Split-Path -Leaf $distRoot))
$exePath = Join-Path $distDir "12306FairTicket.exe"

$requiredPaths = @(
    (Join-Path $projectRoot "gui.py"),
    (Join-Path $projectRoot "assets"),
    (Join-Path $projectRoot "assets\app.qss"),
    (Join-Path $projectRoot "assets\app_icon.svg"),
    (Join-Path $projectRoot "assets\calendar.svg"),
    (Join-Path $projectRoot "assets\check.svg"),
    (Join-Path $projectRoot "assets\stations_snapshot.json"),
    $specPath,
    (Join-Path $projectRoot "README.md"),
    (Join-Path $projectRoot "docs\四步订票流程操作说明.md"),
    (Join-Path $projectRoot "docs\THIRD_PARTY_NOTICES.md")
)

foreach ($requiredPath in $requiredPaths) {
    if (-not (Test-Path -LiteralPath $requiredPath)) {
        throw "Required build input not found: $requiredPath"
    }
}

Get-Command -Name $Python -ErrorAction Stop | Out-Null
$pythonVersion = & $Python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($LASTEXITCODE -ne 0) {
    throw "Unable to query the selected Python interpreter."
}
if ($pythonVersion.Trim() -ne "3.12") {
    throw "Python 3.12 is required; selected interpreter reports $($pythonVersion.Trim())."
}

& $Python -c "import PyInstaller, PySide6, json5; print(f'PyInstaller {PyInstaller.__version__}; PySide6 {PySide6.__version__}; json5 {json5.__version__}')"
if ($LASTEXITCODE -ne 0) {
    throw "Build dependencies are missing. Run: python -m pip install -r requirements/dev.txt"
}

Push-Location -LiteralPath $projectRoot
try {
    if (-not $SkipTests) {
        $previousQtPlatform = $env:QT_QPA_PLATFORM
        try {
            $env:QT_QPA_PLATFORM = "offscreen"
            $testTemp = Join-Path $projectRoot (".pytest-tmp-" + [Guid]::NewGuid().ToString("N"))
            & $Python -m pytest tests -q -p no:cacheprovider --basetemp $testTemp
            $testExitCode = $LASTEXITCODE
        }
        finally {
            $env:QT_QPA_PLATFORM = $previousQtPlatform
        }
        if ($testExitCode -ne 0) {
            throw "Unit tests failed; packaging was not started."
        }
    }

    & $Python -m PyInstaller --noconfirm --clean --distpath $distRoot --workpath $workRoot $specPath
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE."
    }

    if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
        throw "Build completed without the expected executable: $exePath"
    }

    $smokeProcess = Start-Process -FilePath $exePath -ArgumentList "--smoke-test" -PassThru -WindowStyle Hidden
    if (-not $smokeProcess.WaitForExit(20000)) {
        Stop-Process -Id $smokeProcess.Id -Force
        throw "Frozen application smoke test timed out after 20 seconds."
    }
    if ($smokeProcess.ExitCode -ne 0) {
        throw "Frozen application smoke test failed with exit code $($smokeProcess.ExitCode)."
    }

    Copy-Item -LiteralPath (Join-Path $projectRoot "README.md") -Destination $distDir -Force
    Copy-Item -LiteralPath (Join-Path $projectRoot "docs") -Destination $distDir -Recurse -Force

    Write-Host "Windows onedir build ready: $exePath"
}
finally {
    Pop-Location
}
