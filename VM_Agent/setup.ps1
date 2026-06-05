# PrZMA VM_Agent setup script.
# Run inside the VM after copying the VM_Agent directory.
#
# Example:
#   Set-ExecutionPolicy -Scope Process Bypass -Force
#   .\setup.ps1
#   .\setup.ps1 -RegisterStartup

param(
  [switch]$RegisterStartup,
  [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

$WorkDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $WorkDir

$VenvDir = Join-Path $WorkDir ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$Requirements = Join-Path $WorkDir "requirements.txt"

Write-Host "[PrZMA VM_Agent] Working directory: $WorkDir"

if (!(Test-Path $VenvPython)) {
  Write-Host "[PrZMA VM_Agent] Creating virtual environment..."
  & $Python -m venv $VenvDir
}

Write-Host "[PrZMA VM_Agent] Upgrading pip..."
& $VenvPython -m pip install --upgrade pip

if (!(Test-Path $Requirements)) {
  throw "Missing requirements file: $Requirements"
}

Write-Host "[PrZMA VM_Agent] Installing Python dependencies..."
& $VenvPython -m pip install -r $Requirements

Write-Host "[PrZMA VM_Agent] Installing Playwright Chromium..."
& $VenvPython -m playwright install chromium

$ConfigPath = Join-Path $WorkDir "vm_agent_config.json"
if (!(Test-Path $ConfigPath)) {
  Write-Host "[PrZMA VM_Agent] Creating default vm_agent_config.json..."
  $DefaultConfig = @{
    host = "0.0.0.0"
    port = 18861
    snapshot_root = (Join-Path $WorkDir "snap_staging")
  }
  $DefaultConfig | ConvertTo-Json -Depth 4 | Set-Content -Path $ConfigPath -Encoding UTF8
}

if ($RegisterStartup) {
  Write-Host "[PrZMA VM_Agent] Registering startup scheduled task..."
  & (Join-Path $WorkDir "init.ps1")
}

Write-Host "[PrZMA VM_Agent] Setup complete."
Write-Host "[PrZMA VM_Agent] Start manually with:"
Write-Host "  .\.venv\Scripts\python.exe .\agent_main.py"
