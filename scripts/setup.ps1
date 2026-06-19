# yatsee_setup.ps1
#
# Bootstrap helper for local YATSEE development on Windows.
#
# This script is a convenience wrapper around:
#   - Python version checks
#   - virtualenv creation
#   - editable package install from pyproject.toml
#   - core system tool checks
#   - optional CUDA detection
#   - optional spaCy model install
#
# pyproject.toml is the source of truth for Python dependencies.

$ErrorActionPreference = "Stop"

Write-Host "Starting YATSEE bootstrap..." -ForegroundColor Cyan

$PythonRequiredMajor = 3
$PythonRequiredMinor = 11
$VenvDir = ".venv"
$DefaultInstallTarget = "full"
$DefaultSpacyModel = "en_core_web_md"

function Write-Section {
    param([string]$Title)
    Write-Host ""
    Write-Host "== $Title ==" -ForegroundColor Cyan
}

function Resolve-PythonCommand {
    if (Get-Command python -ErrorAction SilentlyContinue) {
        return "python"
    }

    if (Get-Command py -ErrorAction SilentlyContinue) {
        return "py"
    }

    Write-Host "ERROR: Python is not installed or not in PATH." -ForegroundColor Red
    exit 1
}

function Test-PythonVersion {
    param([string]$PythonCmd)

    $pythonVersion = & $PythonCmd -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    $isVersionOk = & $PythonCmd -c "import sys; print(sys.version_info >= ($PythonRequiredMajor, $PythonRequiredMinor))"

    if ($isVersionOk -ne "True") {
        Write-Host "ERROR: Python $PythonRequiredMajor.$PythonRequiredMinor+ is required. Found $pythonVersion" -ForegroundColor Red
        exit 1
    }

    Write-Host "Python $pythonVersion OK"
    return $pythonVersion
}

function Test-VenvModule {
    param([string]$PythonCmd)

    try {
        & $PythonCmd -m venv --help > $null
        Write-Host "venv module available"
    }
    catch {
        Write-Host "ERROR: Python venv module is not available." -ForegroundColor Red
        exit 1
    }
}

function Test-RepoFiles {
    Write-Section "Repository checks"

    if (!(Test-Path "pyproject.toml")) {
        Write-Host "ERROR: pyproject.toml not found. Run this from the YATSEE repository root." -ForegroundColor Red
        exit 1
    }

    Write-Host "Found pyproject.toml"
}

function Test-SystemTool {
    param(
        [string]$ToolName,
        [string]$Purpose
    )

    $cmd = Get-Command $ToolName -ErrorAction SilentlyContinue
    if ($null -eq $cmd) {
        Write-Host "WARNING: $ToolName not found." -ForegroundColor Yellow
        Write-Host "  Needed for: $Purpose"
        Write-Host "  Suggestion: install with Winget, Scoop, or Chocolatey"
    }
    else {
        Write-Host "$ToolName found: $($cmd.Source)"
    }
}

function Test-SystemTools {
    Write-Section "System tool checks"

    Test-SystemTool -ToolName "ffmpeg" -Purpose "audio formatting and transcoding"
}

function New-AndActivateVenv {
    param([string]$PythonCmd)

    Write-Section "Virtual environment"

    if (!(Test-Path $VenvDir)) {
        Write-Host "Creating virtual environment..."
        & $PythonCmd -m venv $VenvDir
        Write-Host "Created virtual environment at $VenvDir"
    }
    else {
        Write-Host "Using existing virtual environment at $VenvDir"
    }

    $activateScript = Join-Path (Get-Location) "$VenvDir\Scripts\Activate.ps1"
    & $activateScript
    Write-Host "Activated virtual environment"
    Write-Host "Remember to run: .\$VenvDir\Scripts\Activate.ps1"
}

function Install-YatseePackage {
    Write-Section "Package install"

    Write-Host "Install targets:"
    Write-Host "  1) base       -> pip install -e ."
    Write-Host "  2) pipeline   -> pip install -e .[pipeline]"
    Write-Host "  3) index      -> pip install -e .[index]"
    Write-Host "  4) search     -> pip install -e .[search]"
    Write-Host "  5) research   -> pip install -e .[research]"
    Write-Host "  6) ui         -> pip install -e .[ui]"
    Write-Host "  7) llamacpp   -> pip install -e .[llamacpp]"
    Write-Host "  8) full       -> pip install -e .[full]"
    Write-Host "  9) skip install"

    $choice = Read-Host "Choose install target [8]"
    if ([string]::IsNullOrWhiteSpace($choice)) {
        $choice = "8"
    }

    switch ($choice) {
        "1" { $pipArgs = @("-e", ".") }
        "2" { $pipArgs = @("-e", ".[pipeline]") }
        "3" { $pipArgs = @("-e", ".[index]") }
        "4" { $pipArgs = @("-e", ".[search]") }
        "5" { $pipArgs = @("-e", ".[research]") }
        "6" { $pipArgs = @("-e", ".[ui]") }
        "7" { $pipArgs = @("-e", ".[llamacpp]") }
        "8" { $pipArgs = @("-e", ".[$DefaultInstallTarget]") }
        "9" {
            Write-Host "Skipping package install"
            return
        }
        default {
            Write-Host "ERROR: Unknown choice '$choice'" -ForegroundColor Red
            exit 1
        }
    }

    python -m pip install --upgrade pip
    Write-Host "Installing with: pip install $($pipArgs -join ' ')"
    python -m pip install @pipArgs
    Write-Host "Package install complete"
}

function Test-ComputeDevice {
    Write-Section "Compute device"

    python -c "import torch" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "torch not installed in this environment; skipping CUDA detection"
        return
    }

    $device = python -c @"
import torch
device = "CPU"
if torch.cuda.is_available():
    device = "CUDA GPU"
print(device)
"@

    Write-Host "Detected processing device: $device"
}

function Install-SpacyModelIfRequested {
    Write-Section "spaCy model"

    python -c "import spacy" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "spaCy not installed in this environment; skipping model install"
        return
    }

    $choice = Read-Host "Check/install spaCy model $DefaultSpacyModel? [Y/n]"
    if ([string]::IsNullOrWhiteSpace($choice)) {
        $choice = "Y"
    }

    if ($choice -notmatch '^[Yy]$') {
        Write-Host "Skipping spaCy model check"
        return
    }

    python -c "import importlib.util; raise SystemExit(0 if importlib.util.find_spec('$DefaultSpacyModel') else 1)" 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "spaCy model $DefaultSpacyModel already available"
    }
    else {
        Write-Host "Installing spaCy model $DefaultSpacyModel..."
        python -m spacy download $DefaultSpacyModel
        Write-Host "spaCy model $DefaultSpacyModel installed"
    }
}

function Show-Summary {
    param([string]$PythonVersion)

    Write-Section "Summary"

    Write-Host "Bootstrap complete"
    Write-Host "  Python version : $PythonVersion"
    Write-Host "  Virtualenv     : $VenvDir"
    Write-Host "  pyproject.toml : present"

    $ffmpegCmd = Get-Command ffmpeg -ErrorAction SilentlyContinue

    Write-Host "  ffmpeg         : $(if ($ffmpegCmd) { $ffmpegCmd.Source } else { 'missing' })"
}

function Show-NextSteps {
    Write-Section "Next steps"

    Write-Host "Common commands:"
    Write-Host ""
    Write-Host "  yatsee --help"
    Write-Host "  yatsee config --help"
    Write-Host "  yatsee config entity list"
    Write-Host "  yatsee config validate"
    Write-Host "  yatsee audio format --help"
    Write-Host "  yatsee audio transcribe --help"
    Write-Host "  yatsee transcript normalize --help"
    Write-Host "  yatsee intel run --help"
    Write-Host ""
    Write-Host "If the console script is not available yet, use:"
    Write-Host ""
    Write-Host "  python -m yatsee.cli.main --help"
    Write-Host ""
    Write-Host "Notes:"
    Write-Host "  - pyproject.toml is the source of truth for Python dependencies"
    Write-Host "  - ffmpeg is needed for audio formatting/transcoding"
    Write-Host "  - raw media should already exist in data/<entity>/downloads or be passed with --input-dir"
    Write-Host "  - acquisition/import tooling is outside the core YATSEE CLI"
    Write-Host "  - transcript/index workflows may require spaCy models and extras"
    Write-Host "  - intel workflows use provider settings such as llm_provider and llm_provider_url in yatsee.toml"
}

Write-Section "Python checks"
$pythonCmd = Resolve-PythonCommand
$pythonVersion = Test-PythonVersion -PythonCmd $pythonCmd
Test-VenvModule -PythonCmd $pythonCmd

Test-RepoFiles
Test-SystemTools
New-AndActivateVenv -PythonCmd $pythonCmd
Install-YatseePackage
Test-ComputeDevice
Install-SpacyModelIfRequested
Show-Summary -PythonVersion $pythonVersion
Show-NextSteps