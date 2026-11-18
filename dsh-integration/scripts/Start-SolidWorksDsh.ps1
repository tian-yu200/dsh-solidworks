[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$DshRoot = "D:\deepseek-harness",
    [string]$DshHome = "$HOME\.dsh",
    [string]$WorkspaceDir = "",
    [switch]$SkipVisionCheck,
    [switch]$Quiet,
    [Parameter(ValueFromRemainingArguments)]
    [string[]]$DshArguments
)

$Dsh = Join-Path $DshRoot "node_modules\.bin\dsh.CMD"
if (-not (Test-Path -LiteralPath $Dsh)) {
    throw "未找到 DSH 启动器 (DSH launcher not found): $Dsh"
}

. (Join-Path $PSScriptRoot "SolidWorksEnvHelpers.ps1")

# Machine-consumed flags (--dump-config, --help, --version) must receive clean stdout without banners.
$PassthroughFlags = @("--dump-config", "--dump-default-config", "-h", "--help", "-V", "--version")
$IsMachinePassthrough = $false
foreach ($Arg in @($DshArguments)) {
    if ($PassthroughFlags -contains $Arg) {
        $IsMachinePassthrough = $true
        break
    }
}

if ($IsMachinePassthrough) {
    & $Dsh --profile solidworks @DshArguments
    exit $LASTEXITCODE
}

# Pre-flight 64-bit check so 32-bit PowerShell never causes confusing COM/PlatformNotSupported errors.
$ArchStatus = Test-SolidWorks64BitEnvironment
if (-not $ArchStatus.Ok) {
    Write-Host "【启动拦截】$($ArchStatus.RemediationZh)" -ForegroundColor Red
    exit 1
}

# Pre-flight vision guard: block known text-only default models (e.g. DeepSeek-V4.1-Flash) before UI launch.
if (-not $SkipVisionCheck) {
    $ConfiguredModel = Get-ConfiguredDefaultModelQuick -DshHome $DshHome -DshRoot $DshRoot
    if ($null -ne $ConfiguredModel -and (Test-KnownTextOnlyModelId -ModelId $ConfiguredModel.model)) {
        Write-Host ""
        Write-Host "======================================================================" -ForegroundColor Red
        Write-Host "【看图建模拦截提示】检测到当前默认模型不支持图片识别！" -ForegroundColor Red
        Write-Host "  当前默认模型：$($ConfiguredModel.provider)/$($ConfiguredModel.model)" -ForegroundColor Yellow
        Write-Host "  原因说明：DeepSeek 纯文本/Flash 系列模型没有视觉识图能力，发送工程图会直接报错或建模失败。" -ForegroundColor Yellow
        Write-Host "  解决办法：请将默认模型切换为支持视觉的多模态模型（如 GPT-6.1-Sol / Claude / GLM 多模态系列）。" -ForegroundColor Green
        Write-Host "  （如仅需纯文字对话修改已有尺寸，可加参数 -SkipVisionCheck 跳过此检查）" -ForegroundColor DarkGray
        Write-Host "======================================================================" -ForegroundColor Red
        exit 1
    }
}

# Resolve non-C workspace directory if specified or previously saved by Setup-SolidWorksEasy.ps1.
$EffectiveWorkspace = ""
if ($WorkspaceDir -and $WorkspaceDir.Trim() -ne "") {
    $EffectiveWorkspace = [System.IO.Path]::GetFullPath($WorkspaceDir.Trim())
    New-Item -ItemType Directory -Force -Path $EffectiveWorkspace | Out-Null
} else {
    $SavedWorkspace = Get-SavedSolidWorksWorkspaceDir -DshHome $DshHome
    if ($SavedWorkspace) {
        $EffectiveWorkspace = $SavedWorkspace
        New-Item -ItemType Directory -Force -Path $EffectiveWorkspace | Out-Null
    }
}

# Eliminate ambient danger-full-access overrides for this process tree.
$PreviousPermissionMode = $env:DSH_PERMISSION_MODE
$HadDangerEnv = ($PreviousPermissionMode -eq "danger-full-access")
$env:DSH_PERMISSION_MODE = "workspace-write"

if (-not $Quiet) {
    Write-Host ""
    Write-Host "======================================================================" -ForegroundColor Cyan
    Write-Host "  正在启动 SolidWorks AI 建模助手 (专属安全配置：solidworks)" -ForegroundColor Cyan
    Write-Host "----------------------------------------------------------------------" -ForegroundColor DarkCyan
    Write-Host "  1. 电脑配置说明：AI 推理在云端运行，本机只需能流畅运行 SolidWorks 即可。" -ForegroundColor Gray
    if ($EffectiveWorkspace) {
        Write-Host "  2. 零件保存目录：$EffectiveWorkspace（优先非 C 盘，不占系统盘空间）" -ForegroundColor Green
    } else {
        Write-Host "  2. 零件保存目录：$(Get-Location)（可通过 Setup-SolidWorksEasy.ps1 设为 D 盘目录）" -ForegroundColor Gray
    }
    Write-Host "  3. 权限安全边界：已启用 workspace-write + 17 工具白名单，无需开启 Full access。" -ForegroundColor Gray
    if ($HadDangerEnv) {
        Write-Host "     （已自动忽略外部环境变量 DSH_PERMISSION_MODE=danger-full-access，消除高危警告）" -ForegroundColor DarkYellow
    }
    Write-Host "  4. 窗口使用提示：下方滚动的英文属于正常后台连接日志，不是报错！" -ForegroundColor Yellow
    Write-Host "     建模期间请将本窗口最小化保留，切勿直接关闭。" -ForegroundColor Yellow
    Write-Host "======================================================================" -ForegroundColor Cyan
    Write-Host ""
}

$PushedLocation = $false
try {
    if ($EffectiveWorkspace) {
        Push-Location -LiteralPath $EffectiveWorkspace
        $PushedLocation = $true
    }
    & $Dsh --profile solidworks @DshArguments
    $ExitCode = $LASTEXITCODE
} finally {
    if ($PushedLocation) {
        Pop-Location
    }
    $env:DSH_PERMISSION_MODE = $PreviousPermissionMode
}
exit $ExitCode
