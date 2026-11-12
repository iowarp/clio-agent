# Small, locked source-installer worker. The caller owns and joins this process.
param(
    [Parameter(Mandatory)][string]$SourceRoot,
    [Parameter(Mandatory)][string]$PythonVersion
)
$ErrorActionPreference = 'Stop'
# Keep uv's concurrent syncs in distinct environments; the shared download cache
# retains uv's own coordination and the versions still come from this checkout.
$env:UV_PROJECT_ENVIRONMENT = Join-Path $SourceRoot '.venv-install-bootstrap'
$env:PYTHONPATH = Join-Path $SourceRoot 'src'
$synced = [Diagnostics.Stopwatch]::StartNew()
& uv sync --project $SourceRoot --locked --only-group install-bootstrap --no-install-project --python $PythonVersion
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
# office_bootstrap reports this beside its sub-jobs.
$env:CLIO_BOOTSTRAP_SYNC_SECONDS = [Math]::Round($synced.Elapsed.TotalSeconds, 1).ToString([Globalization.CultureInfo]::InvariantCulture)
& uv run --project $SourceRoot --frozen --no-sync python -B -m clio_agent.runtime.office_bootstrap
exit $LASTEXITCODE
