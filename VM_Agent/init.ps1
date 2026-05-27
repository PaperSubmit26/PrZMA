# PrZMA VM_Agent - Register scheduled task to run agent_main.py at Windows startup.
# Run this script once on the VM (e.g. as Administrator) so the agent starts automatically on boot.
# Prerequisite: VM_Agent is deployed at C:\Users\VM Agent\VM_Agent (see README).

$TaskName = "PrZMA_VM_Agent"
$WORKDIR = "C:\Users\VM Agent\VM_Agent"
$PY     = Join-Path $WORKDIR ".venv\Scripts\python.exe"
$SCRIPT = Join-Path $WORKDIR "agent_main.py"

# Logging
$LOGDIR = Join-Path $WORKDIR "logs"
New-Item -ItemType Directory -Force $LOGDIR | Out-Null
$LOG = Join-Path $LOGDIR "agent_main_startup.log"

$Action = New-ScheduledTaskAction `
  -Execute "C:\Windows\System32\cmd.exe" `
  -Argument "/c ""$PY"" -u -File ""$SCRIPT"" >> ""$LOG"" 2>&1"

$Trigger = New-ScheduledTaskTrigger -AtStartup

$Principal = New-ScheduledTaskPrincipal `
  -UserId "SYSTEM" `
  -LogonType ServiceAccount `
  -RunLevel Highest

# Prevent task from not running due to power/conditions
$Settings = New-ScheduledTaskSettingsSet `
  -StartWhenAvailable `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask `
  -TaskName $TaskName `
  -Action $Action `
  -Trigger $Trigger `
  -Principal $Principal `
  -Settings $Settings `
  -Force

Write-Host "Scheduled task '$TaskName' registered. agent_main.py will run at startup; output in $LOG"
