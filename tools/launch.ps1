param(
    [switch]$NoLaunch,
    [switch]$NoBrowser,
    [switch]$Stop,
    [ValidateSet('auto', 'china', 'official')][string]$DownloadSource,
    [ValidateRange(1024, 65515)][int]$Port = 8000
)

$ErrorActionPreference = 'Stop'
$env:PSModulePath = (Join-Path $PSHOME 'Modules') + ';' + $env:PSModulePath
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$workspace = Split-Path -Parent $PSScriptRoot
$stateDir = Join-Path $workspace '.cache\launcher'
$python = Join-Path $workspace '.venv\Scripts\python.exe'
$env:UV_CACHE_DIR = Join-Path $workspace '.cache\uv'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $workspace '.cache\python'
$env:UV_PYTHON_PREFERENCE = 'only-managed'
$env:UV_PYTHON_NO_REGISTRY = '1'
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $workspace '.cache\browsers'
$env:PIP_CACHE_DIR = Join-Path $workspace '.cache\pip'
$utf8 = New-Object System.Text.UTF8Encoding($false)
$installLock = $null
$transcribing = $false
$exitCode = 0
. (Join-Path $PSScriptRoot 'install_sources.ps1')

function Read-State($Path) {
    if (Test-Path -LiteralPath $Path) {
        try { return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json }
        catch { return $null }
    }
    return $null
}
function Save-State($Path, $Value) {
    [IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json -Depth 5), $utf8)
}
function Run-Step($Program, $Arguments, $Failure) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw $Failure }
}
function Get-Health([int]$Number) {
    try { return Invoke-RestMethod -Uri "http://127.0.0.1:$Number/api/health" -TimeoutSec 2 }
    catch { return $null }
}

try {
    Set-Location -LiteralPath $workspace
    if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64') {
        throw '此安装入口适用于 Windows 10/11 x64。'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $workspace 'pyproject.toml'))) {
        throw '请先解压整个发布包，再双击“启动 PaperLens.cmd”。'
    }
    New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
    $log = Join-Path $stateDir ("setup-" + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
    Start-Transcript -LiteralPath $log | Out-Null
    $transcribing = $true
    Write-Host "`nPaperLens · 安装与启动`n" -ForegroundColor Cyan
    try {
        $installLock = [IO.File]::Open((Join-Path $stateDir 'install.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    } catch { throw '另一个安装窗口正在运行，请等待它完成。' }
    $networkFile = Join-Path $stateDir 'network.json'
    $downloadProfile = Resolve-DownloadSource $DownloadSource $networkFile
    if (-not $Stop) {
        Save-State $networkFile @{ download_source = $downloadProfile }
        Write-Host "下载策略：$downloadProfile（auto：官方优先；china：国内优先；official：仅官方）。"
    }

    $version = [regex]::Match((Get-Content pyproject.toml -Raw), '(?m)^version\s*=\s*"([^"]+)"').Groups[1].Value
    $serverFile = Join-Path $stateDir 'server.json'
    $server = Read-State $serverFile
    if ($Stop) {
        if (-not $server) {
            Write-Host '此安装目录没有记录正在运行的 PaperLens 服务。'
            exit 0
        }
        if ($server.workspace -ne $workspace) { throw '服务记录不属于当前安装目录，未停止任何进程。' }
        $running = Get-CimInstance Win32_Process -Filter ("ProcessId=" + [int]$server.pid)
        if (-not $running) {
            Remove-Item -LiteralPath $serverFile
            Write-Host 'PaperLens 服务已停止。'
            exit 0
        }
        if ($running.ExecutablePath -ne $python -or $running.CommandLine -notmatch '\-m\s+paperlens\s+serve') {
            throw '记录中的进程身份不匹配，未停止任何进程。'
        }
        $health = Get-Health $server.port
        if (-not $health.ok -or $health.features -notcontains 'library' -or $health.version -ne $server.version) {
            throw '无法确认服务身份，请检查当前安装目录的日志。'
        }
        if ($health.running_jobs -gt 0 -or $health.queued_jobs -gt 0) {
            throw '仍有正在处理或排队的论文。请先在网页暂停或取消任务，再停止服务。'
        }
        # Windows venv Python uses a launcher parent; stop its verified child tree too.
        & taskkill.exe /PID ([int]$server.pid) /T /F | Out-Null
        if ($LASTEXITCODE -ne 0) { throw '服务未能停止，请检查当前安装目录的日志。' }
        Remove-Item -LiteralPath $serverFile
        Write-Host 'PaperLens 服务已停止，现在可以升级。'
        exit 0
    }
    $installedFile = Join-Path $stateDir 'installed.json'
    $installed = Read-State $installedFile
    $requirements = 'requirements.txt'
    $fingerprint = $workspace + '|' + (Get-FileHash $requirements -Algorithm SHA256).Hash + '|' + (Get-FileHash pyproject.toml -Algorithm SHA256).Hash
    $ready = $installed -and $installed.fingerprint -eq $fingerprint -and (Test-Path -LiteralPath $python)
    if ($ready) {
        & $python -c "import paperlens, fastapi, pdfplumber, playwright"
        $ready = $LASTEXITCODE -eq 0
    }
    if (-not $ready) {
        Write-Host '[1/4] 准备安装工具。首次使用需要联网。'
        $uvVersion = '0.12.23'
        $uvHash = 'fb8a4117a5224d73abe2204a1e744ae34b544f1b9a0b761576851deaa7215c12'
        $uvDir = Join-Path $stateDir "uv-$uvVersion"
        $uvZip = Join-Path $stateDir "uv-$uvVersion.zip"
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Install-UvBootstrap $downloadProfile $uvZip $uvVersion $uvHash
        if ((Get-FileHash -LiteralPath $uvZip -Algorithm SHA256).Hash -ne $uvHash) {
            throw '安装工具下载不完整，请重新双击启动。'
        }
        Expand-Archive -LiteralPath $uvZip -DestinationPath $uvDir -Force
        $uv = (Get-ChildItem -LiteralPath $uvDir -Recurse -File -Filter 'uv.exe' | Select-Object -First 1).FullName
        if (-not $uv) { throw '安装工具不完整，请重新解压发布包。' }
        Write-Host '[2/4] 准备本目录的 Python 环境。'
        if (-not (Test-Path -LiteralPath $python)) {
            Install-ManagedPython $uv $downloadProfile $stateDir
            Run-Step $uv @('--no-config', 'venv', '--python', '3.12', '--no-python-downloads', '.venv') 'Python 虚拟环境准备失败，请查看日志后重试。'
        }
        Write-Host '[3/4] 安装所需组件。下载中请保留此窗口。'
        & $python -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('pip') else 1)"
        if ($LASTEXITCODE -ne 0) {
            Invoke-UvSourceStep -Uv $uv -Arguments @('pip', 'install', '--python', $python, 'pip') -Profile $downloadProfile -Stage '兼容安装工具准备'
        }
        $packageArguments = @('tools/setup_packages.py', '--uv', $uv, '--python', $python, '--requirements', $requirements, '--root', $workspace)
        Invoke-UvSourceStep -Uv $python -Arguments $packageArguments -Profile $downloadProfile -Stage '组件安装' -PackageInstall
        Run-Step $uv @('--no-config', 'pip', 'check', '--python', $python) '组件检查未通过，详情见安装日志。'
        Save-State $installedFile @{ fingerprint = $fingerprint; runtime = 'ai-direct'; version = $version }
    } else { Write-Host '运行环境已准备好，无需重复安装。' }

    Write-Host '[4/4] AI 直读环境已就绪，无需下载本地 OCR 或公式模型。'
    $browsers = @(
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
        "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
        "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
    )
    if (-not ($browsers | Where-Object { Test-Path -LiteralPath $_ }) -and -not (Get-ChildItem -LiteralPath $env:PLAYWRIGHT_BROWSERS_PATH -Filter 'chromium*' -ErrorAction SilentlyContinue)) {
        Write-Host '准备生成阅读页面所需的浏览器。'
        Run-Step $python @('-m', 'playwright', 'install', 'chromium') '浏览器组件下载失败，请重试。'
    }
    # Validate with the installed application, including nested settings and custom
    # field validators. Never print Pydantic's input values (they may be secrets).
    $configurationCheck = @'
import sys
from paperlens.config import Settings
try:
    Settings()
except Exception:
    print('Configuration validation failed. Check .env field names and value formats.')
    sys.exit(1)
'@
    Run-Step $python @('-c', $configurationCheck) '配置校验未通过，请在高级设置或 .env 中修正后重试。'
    if ($NoLaunch) { Write-Host '安装完成。'; exit 0 }

    if ($server -and $server.workspace -eq $workspace -and (Get-Process -Id $server.pid -ErrorAction SilentlyContinue)) {
        $health = Get-Health $server.port
        if ($health.ok -and $health.version -eq $version -and $health.features -contains 'library') {
            Write-Host "PaperLens 已在运行：http://127.0.0.1:$($server.port)"
            if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$($server.port)" }
            exit 0
        }
    }

    $available = $null
    foreach ($candidate in $Port..($Port + 20)) {
        $listener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, $candidate)
        try { $listener.Start(); $available = $candidate; break } catch {} finally { $listener.Stop() }
    }
    if ($null -eq $available) { throw '暂时没有可用端口，请稍后重试。' }
    $stdout = Join-Path $stateDir "server-$available.log"
    $stderr = Join-Path $stateDir "server-$available-error.log"
    $process = Start-Process -FilePath $python -ArgumentList @('-m', 'paperlens', 'serve', '--port', $available) -WorkingDirectory $workspace -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    $url = "http://127.0.0.1:$available"
    $healthy = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        if ($process.HasExited) { throw "启动失败，请查看 $stderr" }
        $health = Get-Health $available
        if ($health.ok -and $health.features -contains 'library') { $healthy = $true; break }
        Start-Sleep -Milliseconds 500
    }
    if (-not $healthy) { throw "启动仍未完成，请稍后打开 $url；日志：$stderr" }
    Save-State $serverFile @{ pid = $process.Id; port = $available; version = $version; workspace = $workspace }
    Write-Host "`nPaperLens 已启动：$url`n以后双击同一个入口即可。" -ForegroundColor Green
    if (-not $NoBrowser) { Start-Process $url }
} catch {
    $exitCode = 1
    Write-Host "`n未能完成：$($_.Exception.Message)" -ForegroundColor Red
    if ($log) { Write-Host "日志位置：$log" }
} finally {
    if ($installLock) { $installLock.Dispose() }
    if ($transcribing) { Stop-Transcript | Out-Null }
}
exit $exitCode
