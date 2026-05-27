# PrZMA/przma_telegram_full_trigger.ps1
# Run full trigger on Telegram Web only (no TMI, no tool spec).
# Bootstrap opens Telegram -> Full Trigger runs on Telegram -> Main loop.
# Uses przma_config.json for vm_boot/discovery; overlay: target_application=telegram_web, run_full_trigger=true.
#
# Prereqs: .env with TELEGRAM_MEETING_CHAT (chat name/username), optionally TELEGRAM_A1_EMAIL, TELEGRAM_A1_PASSWORD, etc.
# Note: Telegram Web usually uses persistent browser session, so login may not be required if already logged in.
#
# Example:
#   .\przma_telegram_full_trigger.ps1
#   .\przma_telegram_full_trigger.ps1 -RunId my_telegram_ft
param(
  [string]$RunId = "telegram_full_trigger_01",
  [string]$TemplateConfig = ".\przma_config.json",
  [string]$Catalog = ".\Snapshot_Engine\artifact_catalog.json",
  [string]$OutDir = ".\runs",
  [string]$InterpreterOutRoot = ".\interpreter_out"
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoRoot

$WorkDir = Join-Path $InterpreterOutRoot "telegram_full_trigger"
$ConfigPath = Join-Path $WorkDir "telegram_full_trigger_config.json"
$RulesPath = Join-Path $WorkDir "rules.json"

if (!(Test-Path $TemplateConfig)) {
  Write-Error "Template config not found: $TemplateConfig"
}

# 1) Build Telegram + full-trigger-only config (merge over przma_config.json)
Write-Host "[*] Building Telegram full-trigger config (no tool spec)"
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
$BuildScript = Join-Path $RepoRoot "scripts\build_telegram_full_trigger_config.py"
if (!(Test-Path $BuildScript)) { throw "Script not found: $BuildScript" }
python $BuildScript
if (!(Test-Path $ConfigPath)) { throw "Config build failed: $ConfigPath not created" }
Write-Host "    $ConfigPath"

# 2) Run main.py (Bootstrap -> Full Trigger -> Main loop)
Write-Host "[*] Running PrZMA main.py (Telegram + full trigger only)"
python -u .\main.py `
  --config $ConfigPath `
  --run-id $RunId `
  --rules $RulesPath `
  --catalog $Catalog `
  --out-dir $OutDir

Write-Host "[*] Done. Run output: $OutDir\run_$RunId"
Write-Host "    Ensure TELEGRAM_MEETING_CHAT is set in .env (chat name/username)"
Write-Host "    Note: Telegram Web uses persistent session; ensure browser is logged in or set credentials"
