# Dot-sourced by launch.ps1. All caches and settings belong to this installation.
function Resolve-DownloadSource([string]$Requested, [string]$StatePath) {
    if ($Requested -in @('auto', 'china', 'official')) { return $Requested }
    if (Test-Path -LiteralPath $StatePath) {
        try {
            $saved = Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($saved.download_source -in @('auto', 'china', 'official')) { return $saved.download_source }
        } catch {}
    }
    return 'auto'
}

function Get-DownloadSourceOrder([string]$Profile) {
    switch ($Profile) {
        'china' { return @('china', 'official') }
        'official' { return @('official') }
        default { return @('official', 'china') }
    }
}

function Get-SourceIndex([string]$Source) {
    if ($Source -eq 'china') { return 'https://pypi.tuna.tsinghua.edu.cn/simple' }
    return 'https://pypi.org/simple'
}

function Get-UvBootstrapUrl([string]$Source, [string]$Version) {
    if ($Source -eq 'official') {
        return 'https://files.pythonhosted.org/packages/af/fd/816f4e8a76197312ec8037369e5b5c8a5ebde7aa118b6250259e031d3444/uv-0.12.23-py3-none-win_amd64.whl'
    }
    # Resolve the pinned wheel from the mirror's standard PyPI simple index.
    # Never substitute an unpinned latest uv executable from a release proxy.
    $index = (Get-SourceIndex $Source).TrimEnd('/') + '/uv/'
    $page = Invoke-WebRequest -UseBasicParsing -Uri $index -TimeoutSec 30
    $wheel = 'uv-' + $Version + '-py3-none-win_amd64.whl'
    foreach ($match in [regex]::Matches($page.Content, 'href=["'']([^"'']+)["'']')) {
        $href = [Net.WebUtility]::HtmlDecode($match.Groups[1].Value)
        $uri = New-Object Uri ([Uri]$index), $href
        if ($uri.Scheme -eq 'https' -and [Uri]::UnescapeDataString($uri.AbsolutePath).EndsWith('/' + $wheel)) {
            return $uri.AbsoluteUri.Split('#')[0]
        }
    }
    throw "Mirror does not contain pinned wheel $wheel."
}

function Receive-VerifiedDownload([string]$Url, [string]$Destination, [string]$Sha256, [switch]$UvPythonArchive) {
    # Python's expected SHA256 lives in the pinned uv binary. In that case uv
    # validates the staged archive itself before accepting the installation.
    if (-not $Sha256 -and -not $UvPythonArchive) { throw 'An expected SHA256 is required.' }
    if ((Test-Path -LiteralPath $Destination) -and ($UvPythonArchive -or (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash -eq $Sha256)) { return }
    $partial = $Destination + '.part'
    # A killed process can leave a complete file before its atomic promotion.
    if ($Sha256 -and (Test-Path -LiteralPath $partial) -and (Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash -eq $Sha256) {
        Move-Item -LiteralPath $partial -Destination $Destination -Force
        return
    }
    if (-not (Get-Command curl.exe -ErrorAction SilentlyContinue)) {
        throw 'Windows curl.exe is required to resume installation downloads.'
    }
    for ($attempt = 0; $attempt -lt 2; $attempt++) {
        $httpStatus = & curl.exe --fail --location --silent --show-error --connect-timeout 20 --max-time 600 --retry 2 --retry-delay 1 --write-out '%{http_code}' --continue-at - --output $partial $Url
        $downloadExit = $LASTEXITCODE
        if ($downloadExit -eq 33 -or "$httpStatus" -eq '416') {
            # A server that ignores Range must never be appended to the old data.
            Write-Host '当前下载服务器不支持续传，重新下载此文件。'
            Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
            continue
        }
        if ($downloadExit -ne 0) { throw "Download interrupted (curl $downloadExit); partial file retained: $partial" }
        if ($Sha256 -and (Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash -ne $Sha256) {
            Remove-Item -LiteralPath $partial -Force
            throw 'Downloaded file failed SHA256 validation; discarded and switching source.'
        }
        Move-Item -LiteralPath $partial -Destination $Destination -Force
        return
    }
    throw 'Download server does not support resuming this file.'
}

function Install-ManagedPython([string]$Uv, [string]$Profile, [string]$StateDirectory) {
    $metadataOverride = $env:UV_PYTHON_DOWNLOADS_JSON_URL
    $astralOverride = $env:UV_ASTRAL_MIRROR_URL
    $pythonOverride = $env:UV_PYTHON_INSTALL_MIRROR
    try {
        Remove-Item Env:UV_PYTHON_DOWNLOADS_JSON_URL -ErrorAction SilentlyContinue
        Remove-Item Env:UV_ASTRAL_MIRROR_URL -ErrorAction SilentlyContinue
        Remove-Item Env:UV_PYTHON_INSTALL_MIRROR -ErrorAction SilentlyContinue
        $existing = & $Uv --no-config python list cpython@3.12 --only-installed --python-preference only-managed --output-format json | ConvertFrom-Json
        if ($LASTEXITCODE -ne 0) { throw 'Unable to inspect managed Python installations.' }
        if ($existing) { return }
        $downloads = & $Uv --no-config python list cpython@3.12 --only-downloads --output-format json | ConvertFrom-Json
        if ($LASTEXITCODE -ne 0) { throw 'Unable to read Python download metadata from uv.' }
        $download = $downloads | Where-Object { $_.implementation -eq 'cpython' -and $_.os -eq 'windows' -and $_.arch -eq 'x86_64' -and $_.variant -eq 'default' } | Select-Object -First 1
        if (-not $download) { throw 'No supported Python 3.12 distribution was found in uv.' }
        $match = [regex]::Match($download.url, '/releases/download/([^/]+)/([^/]+)$')
        if (-not $match.Success) { throw 'Unexpected Python distribution URL in uv metadata.' }
        $release = $match.Groups[1].Value
        $filename = [Uri]::UnescapeDataString($match.Groups[2].Value)
        $mirrorDirectory = Join-Path $StateDirectory 'downloads/python'
        $archiveDirectory = Join-Path $mirrorDirectory $release
        New-Item -ItemType Directory -Path $archiveDirectory -Force | Out-Null
        $archive = Join-Path $archiveDirectory $filename
        $fileMirror = ([Uri]($mirrorDirectory.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar)).AbsoluteUri
        foreach ($source in (Get-DownloadSourceOrder $Profile)) {
            Write-Host "Python 下载源：$source；中断后可从本目录续传。"
            $url = $download.url
            if ($source -eq 'china') {
                $url = 'https://mirrors.ustc.edu.cn/github-release/astral-sh/python-build-standalone/' + $release + '/' + $match.Groups[2].Value
            }
            try {
                Receive-VerifiedDownload -Url $url -Destination $archive -UvPythonArchive
                & $Uv --no-config python install $download.key --mirror $fileMirror --no-bin --no-registry
                if ($LASTEXITCODE -eq 0) { return }
                # uv rejects a mismatched checksum before installing. Discard a
                # completed invalid archive so the next source can replace it.
                Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
                throw 'uv could not validate or install the staged Python archive.'
            } catch { Write-Host "Python 准备失败（$source）：$($_.Exception.Message)" }
        }
        throw 'Python 下载或校验未完成。请检查日志或切换下载源后重试。'
    } finally {
        $env:UV_PYTHON_DOWNLOADS_JSON_URL = $metadataOverride
        $env:UV_ASTRAL_MIRROR_URL = $astralOverride
        $env:UV_PYTHON_INSTALL_MIRROR = $pythonOverride
    }
}

function Install-UvBootstrap([string]$Profile, [string]$Destination, [string]$Version, [string]$Sha256) {
    if ((Test-Path -LiteralPath $Destination) -and (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash -eq $Sha256) { return }
    $lastFailure = ''
    foreach ($source in (Get-DownloadSourceOrder $Profile)) {
        Write-Host "安装工具下载源：$source"
        try {
            $url = Get-UvBootstrapUrl $source $Version
            Receive-VerifiedDownload $url $Destination $Sha256
            return
        } catch {
            $lastFailure = $_.Exception.Message
            Write-Host "安装工具下载失败（$source）：$lastFailure"
        }
    }
    throw "安装工具下载未完成。已保留可续传内容；可切换下载源后重试。$lastFailure"
}

function Invoke-UvSourceStep([string]$Uv, [string[]]$Arguments, [string]$Profile, [string]$Stage, [switch]$PackageInstall) {
    # Explicit policy should not inherit unrelated global uv/pip index settings.
    $keys = @('UV_PYTHON_INSTALL_MIRROR', 'UV_INDEX', 'UV_DEFAULT_INDEX', 'UV_INDEX_URL', 'UV_EXTRA_INDEX_URL', 'UV_FIND_LINKS', 'PIP_INDEX_URL', 'PIP_EXTRA_INDEX_URL', 'PIP_FIND_LINKS', 'UV_HTTP_CONNECT_TIMEOUT', 'UV_HTTP_TIMEOUT', 'UV_HTTP_RETRIES')
    $original = @{}
    foreach ($key in $keys) {
        $original[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
        [Environment]::SetEnvironmentVariable($key, $null, 'Process')
    }
    try {
        $env:UV_HTTP_CONNECT_TIMEOUT = '20'
        $env:UV_HTTP_TIMEOUT = '90'
        $env:UV_HTTP_RETRIES = '2'
        foreach ($source in (Get-DownloadSourceOrder $Profile)) {
            Write-Host "${Stage}：$source"
            if ($PackageInstall) {
                $sourceArguments = $Arguments + @('--index', (Get-SourceIndex $source))
            } else {
                $sourceArguments = @('--no-config') + $Arguments
                # Optional indexes may use a dedicated distribution endpoint. Its
                # transitive packages can be older than our pinned PyPI packages;
                # match pip's candidate selection over these trusted indexes.
                $sourceArguments += @('--default-index', (Get-SourceIndex $source), '--index-strategy', 'unsafe-best-match')
            }
            & $Uv @sourceArguments
            if ($LASTEXITCODE -eq 0) { return }
            Write-Host "${Stage}失败（$source），将按所选策略尝试下一个下载源；已完成缓存保留。"
        }
        throw "${Stage}未完成。请检查日志或切换下载源后重试；已完成缓存保留。"
    } finally {
        foreach ($key in $keys) { [Environment]::SetEnvironmentVariable($key, $original[$key], 'Process') }
    }
}
