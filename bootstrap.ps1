param([switch]$SelfCheck, [switch]$RuntimeOnly, [switch]$DefineOnly)
$ErrorActionPreference = 'Stop'

function Test-BookingPython([string]$Path) {
    if (-not $Path -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    try {
        $info = New-Object Diagnostics.ProcessStartInfo
        $info.FileName = $Path
        $info.Arguments = '-E -s -B -c "import sys,struct,tkinter,ssl; assert sys.version_info >= (3,11) and struct.calcsize(''P'') == 8; r=tkinter.Tk();r.withdraw();r.update_idletasks();r.destroy();print(''READY'')"'
        $info.UseShellExecute = $false
        $info.CreateNoWindow = $true
        $info.RedirectStandardOutput = $true
        $info.RedirectStandardError = $true
        $process = [Diagnostics.Process]::Start($info)
        try {
            if (-not $process.WaitForExit(15000)) { $process.Kill(); return $false }
            $output = $process.StandardOutput.ReadToEnd()
            return ($process.ExitCode -eq 0 -and $output.Trim() -eq 'READY')
        } finally { $process.Dispose() }
    } catch { return $false }
}

function Get-BookingDigest([string]$Path) {
    $sha = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($Path)
    try { return [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
    finally { $stream.Dispose(); $sha.Dispose() }
}

function Expand-BookingRuntime([string]$Archive, [string]$Target) {
    if ((Get-BookingDigest $Archive) -ne '5e3438e2e568ab3926e27c43b551e26a2a333b0020944516fb5002524fae3ccf') {
        throw '运行环境校验失败，请重新运行一键启动。'
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        foreach ($entry in $zip.Entries) {
            if ($entry.FullName -notmatch '^\.runtime/' -or $entry.FullName -match '(^|/)\.\.(/|$)|\\|:') {
                throw '运行环境压缩包路径无效，已停止。'
            }
        }
    } finally { $zip.Dispose() }
    [IO.Compression.ZipFile]::ExtractToDirectory($Archive, $Target)
}

function Start-BookingAssistant {
    $root = if ($env:HDU_BOOTSTRAP_ROOT) { $env:HDU_BOOTSTRAP_ROOT } else { $PSScriptRoot }
    $root = [IO.Path]::GetFullPath($root).TrimEnd('\')
    if (-not [Environment]::Is64BitOperatingSystem) { throw '需要 Windows 10/11 64 位系统。' }
    $mutex = New-Object Threading.Mutex($false, 'Local\HDUVenueAssistantEnvironmentSetup')
    $locked = $false
    try {
        try { $locked = $mutex.WaitOne(300000) } catch [Threading.AbandonedMutexException] { $locked = $true }
        if (-not $locked) { throw '另一个窗口正在准备环境，请稍后重新打开。' }
        $python = Join-Path $root '.runtime\python.exe'
        if (-not (Test-BookingPython $python)) {
            $python = $null
            if (-not $RuntimeOnly) {
                foreach ($hive in @('HKCU:', 'HKLM:')) {
                    foreach ($version in @('3.14', '3.13', '3.12', '3.11')) {
                        $key = "$hive\Software\Python\PythonCore\$version\InstallPath"
                        if (Test-Path $key) {
                            $candidate = Join-Path (Get-Item $key).GetValue('') 'python.exe'
                            if (Test-BookingPython $candidate) { $python = $candidate; break }
                        }
                    }
                    if ($python) { break }
                }
            }
            if (-not $python) {
                Write-Host '首次使用：正在自动准备运行环境，请稍候。' -ForegroundColor Cyan
                $work = Join-Path $root ('制作记录\环境准备\' + [Guid]::NewGuid().ToString('N'))
                [IO.Directory]::CreateDirectory($work) | Out-Null
                $archive = Join-Path $work 'runtime.zip'
                $uri = 'https://github.com/xxby-cmd/hdu-badminton-booking/releases/download/runtime-win64-py313-20261117/hdu-runtime-win64.zip'
                [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
                $bundled = Join-Path $root 'runtime\hdu-runtime-win64.zip'
                $downloaded = $false
                if (Test-Path -LiteralPath $bundled -PathType Leaf) {
                    Copy-Item -LiteralPath $bundled -Destination $archive
                    $downloaded = $true
                }
                for ($attempt = 1; $attempt -le 3 -and -not $downloaded; $attempt++) {
                    try {
                        $request = [Net.HttpWebRequest]::Create($uri)
                        $request.Timeout = 15000
                        $request.ReadWriteTimeout = 15000
                        $response = $request.GetResponse()
                        try {
                            $inputStream = $response.GetResponseStream()
                            $outputStream = [IO.File]::Create($archive)
                            try { $inputStream.CopyTo($outputStream) } finally { $outputStream.Dispose(); $inputStream.Dispose() }
                        } finally { $response.Dispose() }
                        if ((Get-BookingDigest $archive) -ne '5e3438e2e568ab3926e27c43b551e26a2a333b0020944516fb5002524fae3ccf') { throw '下载校验失败' }
                        $downloaded = $true; break
                    } catch { if ($attempt -lt 3) { Write-Host '下载未完成，正在重试…'; Start-Sleep -Seconds 2 } }
                }
                if (-not $downloaded) { throw '无法下载运行环境。请检查网络是否能访问 GitHub Releases，恢复网络后再次双击启动。' }
                Expand-BookingRuntime $archive $work
                $candidate = Join-Path $work '.runtime\python.exe'
                if (-not (Test-BookingPython $candidate)) { throw '运行环境自检未通过，请检查系统或杀毒软件拦截。' }
                $destination = [IO.Path]::GetFullPath((Join-Path $root '.runtime'))
                if ($destination -ne (Join-Path $root '.runtime') -or -not $work.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) { throw '环境目标目录校验失败。' }
                if (Test-Path -LiteralPath $destination) {
                    Move-Item -LiteralPath $destination -Destination (Join-Path $work 'previous-runtime')
                }
                Move-Item -LiteralPath (Join-Path $work '.runtime') -Destination $destination
                Remove-Item -LiteralPath $archive
                $python = Join-Path $destination 'python.exe'
            }
        }
    } finally {
        if ($locked) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
    if ($SelfCheck) {
        & $python -E -s -B (Join-Path $root 'startup.py') --self-check
        if ($LASTEXITCODE -ne 0) { throw '助手启动自检失败。' }
    } else {
        $windowed = Join-Path (Split-Path $python) 'pythonw.exe'
        $start = New-Object Diagnostics.ProcessStartInfo
        $start.FileName = $windowed
        $start.Arguments = '-E -s -B "' + (Join-Path $root 'startup.py') + '"'
        $start.WorkingDirectory = $root
        $start.UseShellExecute = $false
        $start.CreateNoWindow = $true
        [Diagnostics.Process]::Start($start) | Out-Null
    }
}

if (-not $DefineOnly) {
    try { Start-BookingAssistant } catch { Write-Host ('启动未完成：' + $_.Exception.Message) -ForegroundColor Red; exit 1 }
}
