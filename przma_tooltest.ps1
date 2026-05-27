# PrZMA/przma_tooltest.ps1
param(
  [string]$RunId = "przma_tooltest_01",

  # Tool meta (optional: if empty, TMI should fall back to .env)
  [string]$ToolName = "",
  [string]$ToolVersion = "",

  # Manual source (optional: if both empty, TMI should fall back to .env)
  [string]$ToolManualUrl = "",
  [string]$ToolManualPath = "",

  # Optional template (reuse education config infra)
  [string]$TemplateConfig = ".\przma_config.json",

  # Paths
  [string]$ActionsJson = ".\shared\actions.json",
  [string]$ArtifactCatalogJson = ".\Snapshot_Engine\artifact_catalog.json",

  # Output roots
  [string]$OutDir = ".\runs",
  [string]$InterpreterOutRoot = ".\interpreter_out",

  # main.py args
  [int]$MaxSteps = 15
)

$ErrorActionPreference = "Stop"

# 0) Based on repo root (script location)
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoRoot

# 1) Run TMI (create tool_plan + interpreted_przma_config + tmi_rules)
# Output directory: .\interpreter_out\<RunId>\
$TmiOutDir = Join-Path $InterpreterOutRoot $RunId
$InterpretedConfig = Join-Path $TmiOutDir "interpreted_przma_config.json"
$TmiRules = Join-Path $TmiOutDir "tmi_rules.json"
$ToolPlan = Join-Path $TmiOutDir "tool_plan.json"
$TmiManifest = Join-Path $TmiOutDir "tmi_manifest.json"

Write-Host "[*] Running TMI -> $TmiOutDir"

$TmiArgs = @(
  "-m", "Tool_Manual_Interpreter.tmi_main",
  "--run-id", $RunId,
  "--out-dir", $TmiOutDir,
  "--actions", $ActionsJson,
  "--catalog", $ArtifactCatalogJson
)

if ($ToolName -ne "")    { $TmiArgs += @("--tool-name", $ToolName) }
if ($ToolVersion -ne "") { $TmiArgs += @("--tool-version", $ToolVersion) }

if ($ToolManualUrl -ne "") {
  $TmiArgs += @("--manual-url", $ToolManualUrl)
} elseif ($ToolManualPath -ne "") {
  $TmiArgs += @("--manual-path", $ToolManualPath)
}

if ($TemplateConfig -ne "" -and (Test-Path $TemplateConfig)) {
  $TmiArgs += @("--template-config", $TemplateConfig)
}

python @TmiArgs

if (!(Test-Path $InterpretedConfig)) { throw "TMI failed: missing $InterpretedConfig" }
if (!(Test-Path $TmiRules))         { throw "TMI failed: missing $TmiRules" }
if (!(Test-Path $ToolPlan))         { Write-Warning "TMI: missing $ToolPlan (not fatal if TMI doesn't emit it yet)" }
if (!(Test-Path $TmiManifest))      { Write-Warning "TMI: missing $TmiManifest (not fatal if TMI doesn't emit it yet)" }

Write-Host "[*] TMI done:"
Write-Host "    - $InterpretedConfig"
Write-Host "    - $TmiRules"

# 2) Tool-testing run (main.py): Run with TMI output
Write-Host "[*] Running PrZMA main.py with tool-testing config"

python .\main.py `
  --config $InterpretedConfig `
  --run-id $RunId `
  --rules $TmiRules `
  --catalog $ArtifactCatalogJson `
  --out-dir $OutDir