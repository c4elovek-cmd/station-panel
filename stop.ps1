# Остановка веб-панели Станции
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$pidFile = Join-Path $here "run\panel.pid"

if (-not (Test-Path $pidFile)) { Write-Host "[i] панель не запущена (нет run\panel.pid)"; exit 0 }

$oldPid = (Get-Content $pidFile -Raw).Trim()
$proc = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
if ($proc) {
    Stop-Process -Id $oldPid -Force
    Write-Host "[+] панель остановлена (PID $oldPid)"
} else {
    Write-Host "[i] процесса $oldPid уже нет"
}
Remove-Item $pidFile -ErrorAction SilentlyContinue
