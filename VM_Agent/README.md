# VM_Agent

Agent process that runs **inside the PrZMA virtual machine(s)**. It exposes an RPyC server so the host-side Automation Agent can drive browser automation and logical snapshot collection on the VM.

## Role

- **Execute actions**: browser (launch, navigate, click, screenshot, etc.), Discord Web, and Telegram Web, on a single UI thread.
- **Collect snapshots**: run the Snapper to gather browser/profile/system artifacts into a zip and manifest, and optionally capture web state (HTML, DOM, screenshot, IndexedDB schema) for schema tracking.

One VM_Agent instance runs per VM; multiple VMs (e.g. A1, A2) each run their own copy of this code and `agent_main.py`.

## Layout

```
VM_Agent/
├── agent_main.py          # Entry point: RPyC server, PrZMAService
├── init.ps1               # Register scheduled task for startup (run once on VM)
├── vm_agent_config.json   # Optional: host, port, snapshot_root
├── shared/
│   └── wire_schemas.py    # ActionRequest, ActionResult, SnapshotPolicy, SnapshotResult, etc.
├── services/
│   ├── browser_service.py # Playwright-based browser and page actions
│   ├── discord_service.py # Discord Web automation
│   ├── telegram_service.py# Telegram Web automation
│   └── snapshot/
│       └── snapper.py     # Logical snapshot collection (zip + manifest)
└── files/                 # Sample artifacts (e.g. forensics) used in runs
    └── forensics/         # triage_notes.txt, timeline.csv, etc.
```

## Deployment

Deploy this folder at **`C:\Users\VM Agent\VM_Agent`** on the VM. The Automation Agent and configs (e.g. `przma_config.json`) expect this path for artifact paths and snapshot staging.

## Running

From the VM (with this directory as the working directory, or with `VM_Agent` on `PYTHONPATH`):

```bash
python agent_main.py
```

Default: listens on `0.0.0.0:18861`. Override via `vm_agent_config.json`:

```json
{
  "host": "0.0.0.0",
  "port": 18861,
  "snapshot_root": "C:\\Users\\VM Agent\\VM_Agent\\snap_staging"
}
```

The host discovers VMs via `vm_endpoints.json` (or equivalent) and connects to each agent’s `host:port` to call `execute_action` and `snapshot_collect`.

## Start at boot (init.ps1)

To have `agent_main.py` run automatically when the VM boots, run **once** on the VM (e.g. as Administrator):

```powershell
cd "C:\Users\VM Agent\VM_Agent"
.\init.ps1
```

This registers a Windows scheduled task **PrZMA_VM_Agent** that:

- Runs at startup (`AtStartup`).
- Uses the Python in `.venv\Scripts\python.exe` and runs `agent_main.py` with unbuffered output (`-u`).
- Appends stdout/stderr to `logs\agent_main_startup.log`.

Requirements: VM_Agent deployed at `C:\Users\VM Agent\VM_Agent` and a virtual environment at `C:\Users\VM Agent\VM_Agent\.venv`. To remove the task: `Unregister-ScheduledTask -TaskName PrZMA_VM_Agent`.

## Exposed RPC (PrZMAService)

| Method | Description |
|--------|-------------|
| `exposed_ping()` | Returns `"pong"` for liveness. |
| `exposed_close_agent(agent_id)` | Closes the browser session for that agent. |
| `exposed_execute_action(req)` | Runs one action (browser / discord / telegram). Request is serialized on the host and executed on a single ActionWorker thread. |
| `exposed_snapshot_collect(policy)` | Runs Snapper with the given policy (layers, paths, etc.); can optionally add `web_state` capture for schema tracking. |

All UI work (Playwright, Discord/Telegram) runs on one thread; RPyC calls are serialized through an internal queue so browser state stays consistent.

## Dependencies

- Python 3 (tested on 3.10+)
- **RPyC** – RPC server used by the host to call into the VM
- **Playwright** – browser automation (Chromium); install with `playwright install chromium`
- No separate `requirements.txt` in this folder; use the project’s top-level dependency list if present.

## Notes

- **Screenshots**: Taken under `_shots/` (or as configured) and referenced in action results as `vm_path` artifacts.
- **Snapshot zip**: Produced by Snapper under the configured staging root; the host retrieves it (or a path) from `snapshot_collect` result.
