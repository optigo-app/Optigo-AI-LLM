# Activate venv and run the backend server
$venvScript = Join-Path $PSScriptRoot ".venv\Scripts\Activate.ps1"
if (Test-Path $venvScript) {
    & $venvScript
} else {
    Write-Error "venv not found at $venvScript"
    exit 1
}

python -m uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload
