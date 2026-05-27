# PrZMA/przma_discord_full_trigger.ps1
# Run full trigger on Discord only (no TMI, no tool spec).
# Bootstrap opens Discord -> Full Trigger runs on Discord -> Main loop.
# Uses przma_config.json for vm_boot/discovery; overlay: target_application=discord_web, run_full_trigger=true.
#
# Prereqs: .env with DISCORD_MEETING_CHANNEL, DISCORD_A1_EMAIL, DISCORD_A1_PASSWORD, DISCORD_A2_EMAIL, DISCORD_A2_PASSWORD (two agents for conversation).
#
# Example:
#   .\przma_discord_full_trigger.ps1
#   .\przma_discord_full_trigger.ps1 -RunId my_discord_ft
param(
  [string]$RunId = "discord_full_trigger_01",
  [string]$TemplateConfig = ".\przma_config.json",
  [string]$Catalog = ".\Snapshot_Engine\artifact_catalog.json",
  [string]$OutDir = ".\runs",
  [string]$InterpreterOutRoot = ".\interpreter_out"
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoRoot

$WorkDir = Join-Path $InterpreterOutRoot "discord_full_trigger"
$ConfigPath = Join-Path $WorkDir "discord_full_trigger_config.json"
$RulesPath = Join-Path $WorkDir "rules.json"

if (!(Test-Path $TemplateConfig)) {
  Write-Error "Template config not found: $TemplateConfig"
}

# 1) Build Discord + full-trigger-only config (merge over przma_config.json)
Write-Host "[*] Building Discord full-trigger config (no tool spec)"
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
$BuildScript = Join-Path $RepoRoot "scripts\build_discord_full_trigger_config.py"
if (!(Test-Path $BuildScript)) { throw "Script not found: $BuildScript" }
python $BuildScript
if (!(Test-Path $ConfigPath)) { throw "Config build failed: $ConfigPath not created" }
Write-Host "    $ConfigPath"

# 2) Run main.py (Bootstrap -> Full Trigger -> Main loop)
Write-Host "[*] Running PrZMA main.py (Discord + full trigger only)"
python .\main.py `
  --config $ConfigPath `
  --run-id $RunId `
  --rules $RulesPath `
  --catalog $Catalog `
  --out-dir $OutDir

Write-Host "[*] Done. Run output: $OutDir\run_$RunId"
Write-Host "    Ensure DISCORD_MEETING_CHANNEL and DISCORD_A1 / DISCORD_A2 credentials are set in .env"
