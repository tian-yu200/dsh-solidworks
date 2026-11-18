[CmdletBinding()]
param(
    [string]$DshHome = "$HOME\.dsh",
    [string]$DshRoot = "D:\deepseek-harness",
    [string]$ProjectRoot = "",
    [string]$Python = "",
    [string]$WorkspaceDir = "",
    [string]$SolidWorksInstallDir = "",
    [string]$DumpPath = "",
    [switch]$NonInteractive,
    [switch]$RebuildExecution,
    [switch]$SkipInstall,
    [switch]$SkipDoctor,
    [switch]$SkipDump,
    [switch]$LaunchAfterSetup,
    [switch]$MigrateCustomPreset,
    [switch]$TakeOverSkillLinks,
    [int]$TestOverrideDotNetRelease = -1,
    [ValidateSet("", "x64", "wow64", "x86os")]
    [string]$TestOverrideArchMode = ""
)

$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; $OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
. (Join-Path $PSScriptRoot "SolidWorksEnvHelpers.ps1")

$ProjectRoot = if ($ProjectRoot) {
    [System.IO.Path]::GetFullPath($ProjectRoot)
} else {
    (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}
$DshHome = [System.IO.Path]::GetFullPath($DshHome)
$DshRoot = [System.IO.Path]::GetFullPath($DshRoot)
if (-not $Python) {
    $Python = Join-Path $ProjectRoot "mcp-server-solidworks-main\adapters\claude\.venv\Scripts\python.exe"
}
$Python = [System.IO.Path]::GetFullPath($Python)

Write-Host ""
Write-Host "========================================================================" -ForegroundColor Cyan
Write-Host "       SolidWorks AI 看图建模助手 · 零基础中文一键环境部署向导          " -ForegroundColor Cyan
Write-Host "========================================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "【定心丸 1 · 电脑配置要求】" -ForegroundColor Green
Write-Host "  AI 算力在云端运行，不消耗本机显卡推理算力——只要电脑能流畅运行 SolidWorks 即可！" -ForegroundColor White
Write-Host "  普通办公笔记本、轻薄本或无独立显卡机器均可稳定使用。" -ForegroundColor Gray
Write-Host ""
Write-Host "【定心丸 2 · 磁盘空间保护】" -ForegroundColor Green
Write-Host "  优先引导您将生成的零件保存在非系统盘（如 D:\SW-AI-Workspace），绝不塞满 C 盘。" -ForegroundColor White
Write-Host ""
Write-Host "【定心丸 3 · 权限与窗口说明】" -ForegroundColor Green
Write-Host "  自动启用 workspace-write 安全工作区模式与 17 工具白名单，无需开启高危 danger-full-access；" -ForegroundColor White
Write-Host "  后续启动时保留的黑色命令行窗口是正常的连接桥梁（滚动的英文日志代表正在工作，不是报错）。" -ForegroundColor Gray
Write-Host "------------------------------------------------------------------------" -ForegroundColor DarkCyan

# Step 1: Pre-flight environment check (64-bit, .NET 4.8, SolidWorks COM, Python venv, C# Execution exe)
Write-Host ""
Write-Host "[步骤 1/4] 正在检查本机运行环境..." -ForegroundColor Cyan

$ArchParams = @{}
if ($TestOverrideArchMode -eq "wow64") {
    $ArchParams = @{ Is64BitOS = $true; Is64BitProcess = $false }
} elseif ($TestOverrideArchMode -eq "x86os") {
    $ArchParams = @{ Is64BitOS = $false; Is64BitProcess = $false }
} elseif ($TestOverrideArchMode -eq "x64") {
    $ArchParams = @{ Is64BitOS = $true; Is64BitProcess = $true }
}
$ArchStatus = Test-SolidWorks64BitEnvironment @ArchParams
if (-not $ArchStatus.Ok) {
    Write-Host "  [×] 64位环境检查未通过：$($ArchStatus.Detail)" -ForegroundColor Red
    Write-Host "  【解决办法】$($ArchStatus.RemediationZh)" -ForegroundColor Yellow
    exit 1
}
Write-Host "  [√] 64位系统与终端环境正常 ($($ArchStatus.Detail))" -ForegroundColor Green

$DotNetStatus = Get-DotNetFramework48Status -OverrideRelease $TestOverrideDotNetRelease
if (-not $DotNetStatus.Ok) {
    Write-Host "  [×] .NET Framework 4.8 检查未通过：$($DotNetStatus.Detail)" -ForegroundColor Red
    Write-Host "  【解决办法】$($DotNetStatus.RemediationZh)" -ForegroundColor Yellow
    exit 1
}
Write-Host "  [√] .NET Framework 运行库正常 ($($DotNetStatus.Detail))" -ForegroundColor Green

$SwCom = Get-SolidWorksComStatus -ExplicitInstallDir $SolidWorksInstallDir
if ($SwCom.Registered) {
    Write-Host "  [√] SolidWorks 64位 COM 组件已就绪 ($($SwCom.ExePath))" -ForegroundColor Green
} else {
    Write-Host "  [!] 未检测到 64位 SolidWorks COM 注册：$($SwCom.RemediationZh)" -ForegroundColor Yellow
    Write-Host "      （向导将继续完成 AI 插件配置；正式建模前请先正常启动一次 SolidWorks）" -ForegroundColor DarkYellow
}

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    Write-Host "  [×] 未找到 Python 虚拟环境：$Python" -ForegroundColor Red
    Write-Host "  【解决办法】请确认项目包完整解压，或在 mcp-server-solidworks-main\adapters\claude 下创建 .venv 并安装 requirements.txt。" -ForegroundColor Yellow
    exit 1
}
& $Python -c "import mcp,httpx,pydantic,PIL,pypdfium2" *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  [×] Python 虚拟环境缺少必要组件 (mcp / httpx / pydantic / Pillow / pypdfium2)。" -ForegroundColor Red
    Write-Host "  【解决办法】请执行：& `"$Python`" -m pip install mcp httpx pydantic Pillow pypdfium2" -ForegroundColor Yellow
    exit 1
}
Write-Host "  [√] Python 编排环境与 PDF/图纸解析组件正常" -ForegroundColor Green

$CsprojPath = Join-Path $ProjectRoot "mcp-server-solidworks-main\execution\solidworks\SolidworksExecution\SolidworksExecution.csproj"
$ExecutionExe = Join-Path $ProjectRoot "mcp-server-solidworks-main\execution\solidworks\SolidworksExecution\bin\Debug\SolidworksExecution.exe"
$PeInfo = Get-ExecutablePeArchitecture -ExePath $ExecutionExe

if ((-not $PeInfo.ValidPe) -or $RebuildExecution) {
    $ReasonText = if ($RebuildExecution) { "收到 -RebuildExecution 指令" } else { "未找到预编译的 SolidworksExecution.exe" }
    Write-Host "  [*] $ReasonText，正在尝试查找 MSBuild 自动编译 64位 (x64/Debug) 执行服务..." -ForegroundColor Cyan
    if (-not (Test-Path -LiteralPath $CsprojPath -PathType Leaf)) {
        Write-Host "  [×] 未找到 C# 工程文件：$CsprojPath，且缺少预编译 SolidworksExecution.exe。" -ForegroundColor Red
        exit 1
    }
    $EffectiveSwInstallDir = if ($SolidWorksInstallDir) { $SolidWorksInstallDir } elseif ($SwCom.InstallDir) { $SwCom.InstallDir } else { "" }
    $BuildResult = Invoke-SolidWorksExecutionBuild -CsprojPath $CsprojPath -SolidWorksInstallDir $EffectiveSwInstallDir
    if (-not $BuildResult.Ok) {
        Write-Host "  [×] 自动编译 SolidworksExecution.exe 未成功：$($BuildResult.Output)" -ForegroundColor Red
        Write-Host "  【解决办法】（任选其一）" -ForegroundColor Yellow
        Write-Host "    1. 推荐：直接使用包含预编译 SolidworksExecution.exe 的完整发行包（无需安装编程工具）；" -ForegroundColor Yellow
        Write-Host "    2. 或安装 Visual Studio 2022 生成工具（勾选“.NET 桌面生成工具”），随后重新运行本向导自动编译。" -ForegroundColor Yellow
        exit 1
    }
    $PeInfo = Get-ExecutablePeArchitecture -ExePath $ExecutionExe
    Write-Host "  [√] 已自动编译生成 64位 SolidworksExecution.exe ($($PeInfo.Architecture))" -ForegroundColor Green
} else {
    Write-Host "  [√] C# 执行服务已就绪 ($($PeInfo.Architecture): $ExecutionExe)" -ForegroundColor Green
}

# Step 2: Configure non-C workspace directory
Write-Host ""
Write-Host "[步骤 2/4] 设置 SolidWorks 零件默认保存工作目录..." -ForegroundColor Cyan

$RecommendedDir = Get-RecommendedWorkspaceDir -DshHome $DshHome -ExplicitDir $WorkspaceDir
$ChosenWorkspace = $RecommendedDir
$CanPrompt = (-not $NonInteractive) -and (-not $WorkspaceDir) -and [Environment]::UserInteractive
if ($CanPrompt) {
    try {
        Write-Host "  推荐工作目录（避免占用 C 盘）：$RecommendedDir" -ForegroundColor Green
        $UserInput = Read-Host "  直接按回车采用推荐目录，或输入其他文件夹路径（如 E:\CAD-Models）"
        if ($UserInput -and $UserInput.Trim() -ne "") {
            $ChosenWorkspace = [System.IO.Path]::GetFullPath($UserInput.Trim())
        }
    } catch {
        $ChosenWorkspace = $RecommendedDir
    }
}

$SystemDrivePrefix = if ($env:SystemDrive) { $env:SystemDrive.TrimEnd('\', '/') + "\" } else { "C:\" }
if ($ChosenWorkspace.StartsWith($SystemDrivePrefix, [System.StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath "D:\")) {
    Write-Host "  [提示] 您当前选择的目录位于系统盘 ($ChosenWorkspace)；如需节省 C 盘空间，可随时重跑本脚本并指定 -WorkspaceDir `"D:\SW-AI-Workspace`"。" -ForegroundColor DarkYellow
}

$WorkspaceConfigFile = Save-SolidWorksWorkspaceConfig -DshHome $DshHome -WorkspaceDir $ChosenWorkspace
Write-Host "  [√] 默认工作目录已创建并记录：$ChosenWorkspace" -ForegroundColor Green

# Step 3: Run Install-SolidWorksDsh.ps1
Write-Host ""
Write-Host "[步骤 3/4] 正在安装并同步 DSH SolidWorks 安全配置与技能链接..." -ForegroundColor Cyan

if ($SkipInstall) {
    Write-Host "  [-] 已按 -SkipInstall 参数跳过安装步骤。" -ForegroundColor Gray
} else {
    $InstallScript = Join-Path $PSScriptRoot "Install-SolidWorksDsh.ps1"
    $InstallArgs = @{
        DshHome = $DshHome
        DshRoot = $DshRoot
        Python = $Python
    }
    if ($MigrateCustomPreset) { $InstallArgs.MigrateCustomPreset = $true }
    if ($TakeOverSkillLinks) { $InstallArgs.TakeOverSkillLinks = $true }

    try {
        & $InstallScript @InstallArgs
    } catch {
        Write-Host "  [×] 安装配置过程中遇到问题：$($_.Exception.Message)" -ForegroundColor Red
        Write-Host "  【提示】若是旧自定义目录保护触发拦截，可在确认备份无误后附加 -MigrateCustomPreset 或 -TakeOverSkillLinks 重试。" -ForegroundColor Yellow
        exit 1
    }
    Write-Host "  [√] DSH SolidWorks 专属配置安装完成（默认安全权限模式：workspace-write）" -ForegroundColor Green
}

# Step 4: Run Doctor-SolidWorksDsh.ps1 (including vision model capability guard)
Write-Host ""
Write-Host "[步骤 4/4] 正在执行全项环境自检与多模态视觉模型校验..." -ForegroundColor Cyan

if ($SkipDoctor) {
    Write-Host "  [-] 已按 -SkipDoctor 参数跳过自检步骤。" -ForegroundColor Gray
} else {
    $DoctorScript = Join-Path $PSScriptRoot "Doctor-SolidWorksDsh.ps1"
    $DoctorParams = @(
        "-DshHome", $DshHome,
        "-DshRoot", $DshRoot,
        "-ProjectRoot", $ProjectRoot,
        "-Python", $Python
    )
    if ($DumpPath) {
        $DoctorParams += @("-DumpPath", $DumpPath)
    }
    if ($SkipDump) {
        $DoctorParams += "-SkipDump"
    }

    # Ensure ambient DSH_PERMISSION_MODE=danger-full-access does not pollute validation
    $PrevPermEnv = $env:DSH_PERMISSION_MODE
    $env:DSH_PERMISSION_MODE = "workspace-write"
    try {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $DoctorScript @DoctorParams
        $DoctorExit = $LASTEXITCODE
    } finally {
        $env:DSH_PERMISSION_MODE = $PrevPermEnv
    }

    # Additional quick check to surface an unmistakable Chinese alert when a text-only model is configured
    $QuickModel = Get-ConfiguredDefaultModelQuick -DshHome $DshHome -DshRoot $DshRoot
    if ($null -ne $QuickModel -and (Test-KnownTextOnlyModelId -ModelId $QuickModel.model)) {
        Write-Host ""
        Write-Host "========================================================================" -ForegroundColor Red
        Write-Host "【重要告警：当前默认模型无法看图建模！】" -ForegroundColor Red
        Write-Host "  检测到默认模型为：$($QuickModel.provider)/$($QuickModel.model)" -ForegroundColor Yellow
        Write-Host "  该模型（如 DeepSeek-V4.1-Flash / 纯文本模型）不支持识别工程图纸图片！" -ForegroundColor Yellow
        Write-Host "  请打开 DSH 界面或 ~/.dsh/settings.yaml，将默认模型切换为支持视觉的多模态模型" -ForegroundColor Green
        Write-Host "  （例如 GPT-6.1-Sol、Claude 多模态系列或 GLM 多模态系列），然后重新运行本向导。" -ForegroundColor Green
        Write-Host "========================================================================" -ForegroundColor Red
        exit 1
    }

    if ($DoctorExit -ne 0) {
        Write-Host ""
        Write-Host "【提示】环境自检发现未就绪项（详见上方中文诊断摘要），请按提示调整后重新运行本向导。" -ForegroundColor Yellow
        exit $DoctorExit
    }
}

$StartScript = Join-Path $PSScriptRoot "Start-SolidWorksDsh.ps1"
Write-Host ""
Write-Host "========================================================================" -ForegroundColor Green
Write-Host "  恭喜！SolidWorks AI 看图建模环境已全部配置就绪！" -ForegroundColor Green
Write-Host "------------------------------------------------------------------------" -ForegroundColor DarkGreen
Write-Host "  零件默认保存位置：$ChosenWorkspace" -ForegroundColor White
Write-Host "  下次一键启动命令：& `"$StartScript`" -DshRoot `"$DshRoot`" -WorkspaceDir `"$ChosenWorkspace`"" -ForegroundColor Cyan
Write-Host "  启动后在浏览器新建会话页面确认选中 'SolidWorks CAD'，上传工程图即可开始建模！" -ForegroundColor White
Write-Host "========================================================================" -ForegroundColor Green

if ($LaunchAfterSetup) {
    & $StartScript -DshRoot $DshRoot -DshHome $DshHome -WorkspaceDir $ChosenWorkspace
    exit $LASTEXITCODE
}
