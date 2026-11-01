$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
& .\.venv\Scripts\python.exe scripts\run_all_local.py @args
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
