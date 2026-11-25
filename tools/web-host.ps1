param(
    [ValidateSet('Start','Stop','Status')][string]$Action = 'Status',
    [string]$SiteOrigin,
    [string]$DataDirectory = (Join-Path $PSScriptRoot '..\.web-service')
)
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
$stateFile = Join-Path $DataDirectory 'status.json'
$siteFile = Join-Path $DataDirectory 'site-origin.txt'
$state = if (Test-Path -LiteralPath $stateFile) {Get-Content -Raw -LiteralPath $stateFile | ConvertFrom-Json}
$owned = if ($state -and $state.pid) {
    $candidate = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$state.pid)"
    if ($candidate -and $candidate.CommandLine.Contains('paperlens.web_agent') -and $candidate.CommandLine.Contains($DataDirectory)) {$candidate}
}
if ($Action -eq 'Status') {
    if ($owned) {$state | Select-Object state,message,site,updated_at}
    else {Write-Output 'PaperLens 网页计算客户端当前未运行。'}
    return
}
if ($Action -eq 'Stop') {
    if ($owned) {
        [IO.File]::WriteAllText((Join-Path $DataDirectory 'stop.request'),'stop')
        Write-Output '已请求停止网页计算客户端。'
    } else {Write-Output '网页计算客户端已经停止。'}
    return
}
if ($owned) {Write-Output '网页计算客户端已在运行。'; return}
if (-not (Test-Path -LiteralPath $python)) {throw '请先通过原版启动器安装 PaperLens 运行环境。'}
if (-not (Test-Path -LiteralPath (Join-Path $DataDirectory 'gateway.key'))) {
    throw '请先完成受保护的数据目录和站点凭据配置。'
}
if ($SiteOrigin) {
    if ($SiteOrigin -notmatch '^https://[a-zA-Z0-9.-]+(:[0-9]+)?/?$') {throw '请填写有效的 HTTPS 站点地址。'}
    [IO.File]::WriteAllText($siteFile,$SiteOrigin.TrimEnd('/'))
} elseif (Test-Path -LiteralPath $siteFile) {$SiteOrigin=(Get-Content -Raw -LiteralPath $siteFile).Trim()}
else {throw '首次启动需指定 -SiteOrigin。'}
$cli=@('-m','paperlens.web_agent','run','--site',$SiteOrigin,'--data',('"{0}"' -f $DataDirectory))
$process=Start-Process -FilePath $python -ArgumentList ($cli -join ' ') -WorkingDirectory $repoRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $DataDirectory 'host.out.log') -RedirectStandardError (Join-Path $DataDirectory 'host.err.log')
Write-Output "已启动仅向私有站点发起 HTTPS 连接的计算客户端（PID $($process.Id)）。"
