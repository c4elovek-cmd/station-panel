param([string]$Zig = 'G:\Users\c4elovek\this\AIFiles\.tools\zig\zig-x86_64-windows-0.14.1\zig.exe', [string]$Target = 'aarch64-linux-musl')
$ErrorActionPreference = 'Stop'
# Все компиляторы запускаются без консольных окон, кэши — на G:.
$build = Join-Path $PSScriptRoot 'build'
[IO.Directory]::CreateDirectory($build) | Out-Null
$env:ZIG_GLOBAL_CACHE_DIR = Join-Path $build 'zig-cache'
$env:ZIG_LOCAL_CACHE_DIR = Join-Path $build 'zig-local'
$info = [Diagnostics.ProcessStartInfo]::new($Zig)
$info.UseShellExecute = $false
$info.CreateNoWindow = $true
$info.RedirectStandardError = $true
$info.RedirectStandardOutput = $true
foreach ($arg in @('cc','-target',$Target,'-std=c11','-Os','-static','-pthread','-Wall','-Wextra','-Werror=implicit-function-declaration')) { $info.ArgumentList.Add($arg) }
foreach ($file in @('main.c','audio.c','sc.c','json.c','hls.c')) { $info.ArgumentList.Add((Join-Path $PSScriptRoot "src\$file")) }
$info.ArgumentList.Add('-o')
$info.ArgumentList.Add((Join-Path $build 'panel-player'))
$p = [Diagnostics.Process]::Start($info)
$stderr = $p.StandardError.ReadToEndAsync()
$stdout = $p.StandardOutput.ReadToEndAsync()
$p.WaitForExit()
Write-Output $stdout.GetAwaiter().GetResult()
Write-Output $stderr.GetAwaiter().GetResult()
if ($p.ExitCode -ne 0) { throw "Сборка завершилась с кодом $($p.ExitCode)" }
Get-Item (Join-Path $build 'panel-player') | Select-Object Name,Length
