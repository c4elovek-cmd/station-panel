# Запуск веб-панели Станции (скрыто, в фоне)
#   .\start.ps1              — запустить (если уже запущена, скажет об этом)
#   .\start.ps1 -Restart     — перезапустить
param([switch]$Restart)

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$pidFile = Join-Path $here "run\panel.pid"
$logFile = Join-Path $here "run\panel.log"
New-Item -ItemType Directory -Force -Path (Join-Path $here "run") | Out-Null

function Stop-Panel {
    if (Test-Path $pidFile) {
        $oldPid = (Get-Content $pidFile -Raw).Trim()
        $proc = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
        if ($proc -and $proc.ProcessName -like 'python*') {
            Stop-Process -Id $oldPid -Force
            Write-Host "[*] остановлена старая панель (PID $oldPid)"
        }
        Remove-Item $pidFile -ErrorAction SilentlyContinue
    }
}

if ($Restart) { Stop-Panel }

if (Test-Path $pidFile) {
    $oldPid = (Get-Content $pidFile -Raw).Trim()
    if (Get-Process -Id $oldPid -ErrorAction SilentlyContinue) {
        Write-Host "[=] панель уже запущена (PID $oldPid). Для перезапуска: .\start.ps1 -Restart"
        exit 0
    }
}

$python = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $python) { $python = (Get-Command python.exe).Source }

$proc = Start-Process -FilePath $python -ArgumentList (Join-Path $here "server.py") `
    -WorkingDirectory $here -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $logFile -RedirectStandardError "$logFile.err"

$proc.Id | Out-File $pidFile -Encoding ascii
Start-Sleep -Seconds 3

$config = $null
for ($i = 0; $i -lt 10 -and -not $config; $i++) {
    try { $config = Get-Content (Join-Path $here "config.json") -Raw | ConvertFrom-Json } catch { Start-Sleep -Milliseconds 500 }
}
$port = if ($config) { $config.panel_port } else { 8787 }

# ждём, пока порт реально начнёт слушаться
$reachable = $false
for ($i = 0; $i -lt 12 -and -not $reachable; $i++) {
    $reachable = Test-NetConnection 127.0.0.1 -Port $port -InformationLevel Quiet -WarningAction SilentlyContinue
    if (-not $reachable) { Start-Sleep -Milliseconds 500 }
}

if ($reachable) {
    Write-Host "[+] панель запущена (PID $($proc.Id)): http://127.0.0.1:$port"
    Write-Host "[i] пароль — в файле password.txt рядом со скриптом"
} else {
    Write-Host "[!] панель не отвечает на порту $port, смотри run\panel.log"
}
