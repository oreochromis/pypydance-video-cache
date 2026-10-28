[CmdletBinding()]
param(
    [string]$Python = "",
    [switch]$InstallDependencies
)

$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot

if ($env:OS -ne "Windows_NT") {
    throw "Windows bundles must be built on Windows. Use --demo from source on another platform."
}

if ([string]::IsNullOrWhiteSpace($Python)) {
    $projectPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
    $Python = if (Test-Path -LiteralPath $projectPython) { $projectPython } else { "python" }
}
elseif (Test-Path -LiteralPath $Python -PathType Leaf) {
    $Python = (Resolve-Path -LiteralPath $Python).Path
}

function Invoke-ProjectPython {
    param([string[]]$Arguments)
    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed with exit code $LASTEXITCODE."
    }
}

function Invoke-ProjectPythonCode {
    # Windows PowerShell 5.1 向原生进程传参会吃掉内嵌双引号，-c 内嵌代码改走临时 .py 文件；
    # 脚本方式下 sys.path[0] 是临时目录而非项目根，用 PYTHONPATH 补回项目根（等价于 -c 的行为）
    param([string]$Code, [string[]]$ExtraArgs = @())
    $tempFile = Join-Path ([System.IO.Path]::GetTempPath()) ("dance-build-" + [guid]::NewGuid().ToString("N") + ".py")
    [System.IO.File]::WriteAllText($tempFile, $Code, (New-Object System.Text.UTF8Encoding($false)))
    $oldPythonPath = $env:PYTHONPATH
    $env:PYTHONPATH = $projectRoot
    try {
        Invoke-ProjectPython -Arguments (@($tempFile) + $ExtraArgs)
    }
    finally {
        $env:PYTHONPATH = $oldPythonPath
        Remove-Item -LiteralPath $tempFile -ErrorAction SilentlyContinue
    }
}

Push-Location $projectRoot
try {
    $runtimeCheck = @'
import sys
import struct
if sys.platform != "win32":
    raise SystemExit("Use a native Windows Python interpreter, not WSL Python.")
if struct.calcsize("P") != 8:
    raise SystemExit("The Windows x64 bundle requires a 64-bit Python interpreter.")
if sys.version_info < (3, 10):
    raise SystemExit("The Qt GUI requires Python 3.10 or newer.")
print("Build interpreter:", sys.executable)
print("Python:", sys.version.split()[0])
'@
    Invoke-ProjectPythonCode -Code $runtimeCheck

    if ($InstallDependencies) {
        Invoke-ProjectPython -Arguments @("-m", "pip", "install", "-r", "requirements-dev.txt")
    }

    $dependencyCheck = @'
from importlib import metadata
from PySide6 import QtCore, QtGui, QtWidgets
from PIL import Image
import PyInstaller
from dance_ui.release_resources import describe_app_icons, describe_cooldown
# Importing openvr loads its native library; do not initialize SteamVR here.
import openvr
print("Bundled cooldown video:", describe_cooldown())
print("Approved application icons:", describe_app_icons())
for name in ("PySide6-Essentials", "Pillow", "PyInstaller", "openvr"):
    print(f"{name}: {metadata.version(name)}")
'@
    Invoke-ProjectPythonCode -Code $dependencyCheck

    Invoke-ProjectPython -Arguments @(
        "-m", "PyInstaller",
        "--clean", "--noconfirm", "--onedir", "--windowed",
        "--name", "VRChat跳舞房助手",
        "--uac-admin",
        "--icon", (Join-Path $projectRoot "assets\app-icon.ico"),
        "--distpath", (Join-Path $projectRoot "dist"),
        "--workpath", (Join-Path $projectRoot "build"),
        "--specpath", (Join-Path $projectRoot "build"),
        "--additional-hooks-dir", (Join-Path $projectRoot "tools\pyinstaller_hooks"),
        "--add-data", ((Join-Path $projectRoot "assets\default_cooldown.mp4") + ";assets"),
        "--add-data", ((Join-Path $projectRoot "assets\app-icon.png") + ";assets"),
        "--add-data", ((Join-Path $projectRoot "assets\app-icon.ico") + ";assets"),
        "dance_cache_gui.py"
    )

    $bundleRoot = Join-Path $projectRoot "dist\VRChat跳舞房助手"
    $bundleDocs = Join-Path $bundleRoot "docs"
    New-Item -ItemType Directory -Force -Path $bundleDocs | Out-Null
    foreach ($fileName in @("README.md", "LICENSE", "启动.bat")) {
        Copy-Item -LiteralPath (Join-Path $projectRoot $fileName) -Destination $bundleRoot
    }
    foreach ($fileName in @("requirements.txt", "requirements-dev.txt")) {
        Copy-Item -LiteralPath (Join-Path $projectRoot $fileName) -Destination $bundleDocs
    }
    Copy-Item -LiteralPath (Join-Path $projectRoot "docs\DEVELOPMENT.md") -Destination $bundleDocs
    Copy-Item -LiteralPath (Join-Path $projectRoot "docs\WINDOWS_PREVIEW.md") -Destination $bundleDocs

    # Preserve the available license/notice files from installed distributions.
    $licenseCollector = @'
from importlib import metadata
from pathlib import Path
import shutil
import sys

destination = Path(sys.argv[1]) / "THIRD_PARTY_LICENSES"
for package in ("PySide6-Essentials", "shiboken6", "Pillow", "PyInstaller", "openvr"):
    distribution = metadata.distribution(package)
    copied = 0
    for entry in distribution.files or ():
        parts = Path(entry).parts
        if not any(p.lower().startswith(("license", "copying", "notice")) for p in parts):
            continue
        source = Path(distribution.locate_file(entry))
        if not source.is_file():
            continue
        safe_parts = [p for p in parts if p not in ("..", ".", "/", "\\")]
        target = destination / package / Path(*safe_parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied += 1
    print(f"{package}: copied {copied} available license/notice files")
'@
    Invoke-ProjectPythonCode -Code $licenseCollector -ExtraArgs @($bundleRoot)

    Write-Host ""
    Write-Host "Bundle created: $bundleRoot"
    Write-Host "Distribute the complete VRChat跳舞房助手 folder, including _internal."
    Write-Host "The executable requests UAC elevation, including with --demo."
    Write-Host "Windows + VRChat end-to-end verification remains a separate release step."
}
finally {
    Pop-Location
}
