# Shared environment, COM, .NET 4.8, MSBuild, workspace, and safety preflight helpers
# for SolidWorks DSH onboarding, diagnostics, and startup scripts.

function Test-SolidWorks64BitEnvironment {
    param(
        [bool]$Is64BitOS = [Environment]::Is64BitOperatingSystem,
        [bool]$Is64BitProcess = [Environment]::Is64BitProcess,
        [string]$Edition = [string]$PSVersionTable.PSEdition
    )
    $Ok = $Is64BitOS -and $Is64BitProcess
    if (-not $Is64BitOS) {
        return [pscustomobject]@{
            Ok = $false
            Is64BitOS = $false
            Is64BitProcess = $Is64BitProcess
            Detail = "32-bit Windows OS detected (SolidWorks requires 64-bit Windows)"
            RemediationZh = "SolidWorks 仅支持 64 位 Windows 操作系统，32 位系统会触发 PlatformNotSupportedException 报错。"
        }
    }
    if (-not $Is64BitProcess) {
        return [pscustomobject]@{
            Ok = $false
            Is64BitOS = $true
            Is64BitProcess = $false
            Detail = "32-bit PowerShell (WOW64) process on 64-bit Windows"
            RemediationZh = "当前打开的是 32 位 PowerShell (x86)，会导致注册表重定向与 64 位 SolidWorks COM 错配（触发 PlatformNotSupportedException）。请改用 64 位 Windows PowerShell (C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe) 重新运行。"
        }
    }
    return [pscustomobject]@{
        Ok = $true
        Is64BitOS = $true
        Is64BitProcess = $true
        Detail = "x64 OS & x64 PowerShell ($Edition)"
        RemediationZh = ""
    }
}

function Get-DotNetFramework48Status {
    param(
        [int]$OverrideRelease = -1
    )
    $Release = 0
    if ($OverrideRelease -ge 0) {
        $Release = $OverrideRelease
    } else {
        try {
            $BaseKey = [Microsoft.Win32.RegistryKey]::OpenBaseKey(
                [Microsoft.Win32.RegistryHive]::LocalMachine,
                [Microsoft.Win32.RegistryView]::Registry64
            )
            $SubKey = $BaseKey.OpenSubKey("SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full")
            if ($null -ne $SubKey) {
                $RawVal = $SubKey.GetValue("Release")
                if ($null -ne $RawVal) {
                    $Release = [int]$RawVal
                }
                $SubKey.Close()
            }
            $BaseKey.Close()
        } catch {
            $Release = 0
        }
    }

    # Official Microsoft release keys: 528040 = .NET Framework 4.8, 533320 = .NET Framework 4.8.1
    $Installed = $Release -ge 528040
    $VersionLabel = if ($Release -ge 533320) {
        "4.8.1+"
    } elseif ($Release -ge 528040) {
        "4.8"
    } elseif ($Release -gt 0) {
        "pre-4.8 (Release $Release)"
    } else {
        "not found"
    }

    if ($Installed) {
        return [pscustomobject]@{
            Ok = $true
            Release = $Release
            VersionLabel = $VersionLabel
            Detail = ".NET Framework $VersionLabel (Release $Release)"
            RemediationZh = ""
        }
    }
    return [pscustomobject]@{
        Ok = $false
        Release = $Release
        VersionLabel = $VersionLabel
        Detail = "missing .NET Framework 4.8 (found: $VersionLabel; required Release >= 528040)"
        RemediationZh = "未检测到 .NET Framework 4.8 运行库（当前：$VersionLabel）。C# 执行桥接服务 SolidworksExecution.exe 需要 .NET Framework 4.8 才能连接 SolidWorks，否则启动失败或报平台不支持异常。请从微软官网下载并安装 Microsoft .NET Framework 4.8 Runtime 后重试。"
    }
}

function Get-ExecutablePeArchitecture {
    param([Parameter(Mandatory)][string]$ExePath)
    if (-not (Test-Path -LiteralPath $ExePath -PathType Leaf)) {
        return [pscustomobject]@{
            Exists = $false
            ValidPe = $false
            Architecture = "missing"
            Prefer32Bit = $false
            Is64BitNative = $false
            Detail = "missing: $ExePath"
        }
    }
    try {
        $Bytes = [System.IO.File]::ReadAllBytes($ExePath)
        if ($Bytes.Length -lt 256 -or $Bytes[0] -ne 0x4D -or $Bytes[1] -ne 0x5A) {
            return [pscustomobject]@{
                Exists = $true
                ValidPe = $false
                Architecture = "invalid"
                Prefer32Bit = $false
                Is64BitNative = $false
                Detail = "not a valid Win32/PE executable: $ExePath"
            }
        }
        $PeOffset = [BitConverter]::ToInt32($Bytes, 0x3C)
        if ($PeOffset -lt 0 -or ($PeOffset + 24) -ge $Bytes.Length) {
            return [pscustomobject]@{
                Exists = $true
                ValidPe = $false
                Architecture = "invalid"
                Prefer32Bit = $false
                Is64BitNative = $false
                Detail = "corrupted PE header: $ExePath"
            }
        }
        $Machine = [BitConverter]::ToUInt16($Bytes, $PeOffset + 4)
        $NumSections = [BitConverter]::ToUInt16($Bytes, $PeOffset + 6)
        $OptHeaderSize = [BitConverter]::ToUInt16($Bytes, $PeOffset + 20)
        $OptOffset = $PeOffset + 24
        $Magic = [BitConverter]::ToUInt16($Bytes, $OptOffset)

        if ($Machine -eq 0x8664 -or $Magic -eq 0x020B) {
            return [pscustomobject]@{
                Exists = $true
                ValidPe = $true
                Architecture = "x64"
                Prefer32Bit = $false
                Is64BitNative = $true
                Detail = "x64 (PE32+) -> $ExePath"
            }
        }

        # Check CLR header flags for AnyCPU vs Prefer32Bit / x86
        $ClrDirOffset = $OptOffset + 96 + (14 * 8)
        $ClrRva = if (($ClrDirOffset + 8) -le $Bytes.Length) { [BitConverter]::ToUInt32($Bytes, $ClrDirOffset) } else { 0 }
        $ClrFlags = 0
        if ($ClrRva -gt 0) {
            $SectionTableOffset = $OptOffset + $OptHeaderSize
            for ($Index = 0; $Index -lt $NumSections; $Index++) {
                $Sec = $SectionTableOffset + ($Index * 40)
                if (($Sec + 40) -gt $Bytes.Length) { break }
                $VirtualSize = [BitConverter]::ToUInt32($Bytes, $Sec + 8)
                $VirtualAddr = [BitConverter]::ToUInt32($Bytes, $Sec + 12)
                $RawPtr = [BitConverter]::ToUInt32($Bytes, $Sec + 20)
                if ($ClrRva -ge $VirtualAddr -and $ClrRva -lt ($VirtualAddr + $VirtualSize)) {
                    $ClrFileOffset = $RawPtr + ($ClrRva - $VirtualAddr)
                    if (($ClrFileOffset + 20) -le $Bytes.Length) {
                        $ClrFlags = [BitConverter]::ToUInt32($Bytes, $ClrFileOffset + 16)
                    }
                    break
                }
            }
        }
        $Req32 = ($ClrFlags -band 0x00000002) -ne 0
        $Pref32 = ($ClrFlags -band 0x00020000) -ne 0
        if ($Pref32) {
            return [pscustomobject]@{
                Exists = $true
                ValidPe = $true
                Architecture = "AnyCPU-Prefer32Bit"
                Prefer32Bit = $true
                Is64BitNative = $false
                Detail = "AnyCPU (Prefer32Bit=True; runs WOW64 unless rebuilt with PlatformTarget=x64) -> $ExePath"
            }
        }
        if ($Req32) {
            return [pscustomobject]@{
                Exists = $true
                ValidPe = $true
                Architecture = "x86"
                Prefer32Bit = $true
                Is64BitNative = $false
                Detail = "32-bit x86 only -> $ExePath"
            }
        }
        return [pscustomobject]@{
            Exists = $true
            ValidPe = $true
            Architecture = "AnyCPU-64"
            Prefer32Bit = $false
            Is64BitNative = $true
            Detail = "AnyCPU (64-bit capable) -> $ExePath"
        }
    } catch {
        return [pscustomobject]@{
            Exists = $true
            ValidPe = $false
            Architecture = "unreadable"
            Prefer32Bit = $false
            Is64BitNative = $false
            Detail = "could not inspect PE header: $($_.Exception.Message)"
        }
    }
}

function Get-SolidWorksComStatus {
    param(
        [string]$ExplicitInstallDir = ""
    )
    # Never invoke Marshal.GetActiveObject here: on PowerShell 7 (.NET Core / .NET 5+) that API
    # throws System.PlatformNotSupportedException! Inspect the 64-bit COM registry view instead.
    $Clsid = ""
    $LocalServerRaw = ""
    $ExePath = ""
    try {
        $Classes64 = [Microsoft.Win32.RegistryKey]::OpenBaseKey(
            [Microsoft.Win32.RegistryHive]::ClassesRoot,
            [Microsoft.Win32.RegistryView]::Registry64
        )
        $ProgIdKey = $Classes64.OpenSubKey("SldWorks.Application\CLSID")
        if ($null -ne $ProgIdKey) {
            $Clsid = [string]$ProgIdKey.GetValue("")
            $ProgIdKey.Close()
        }
        if ($Clsid) {
            $ServerKey = $Classes64.OpenSubKey("CLSID\$Clsid\LocalServer32")
            if ($null -ne $ServerKey) {
                $LocalServerRaw = [string]$ServerKey.GetValue("")
                $ServerKey.Close()
            }
        }
        $Classes64.Close()
    } catch {
        # Fallback handled below.
    }

    if ($LocalServerRaw) {
        if ($LocalServerRaw -match '^"([^"]+)"') {
            $ExePath = $Matches[1]
        } elseif ($LocalServerRaw -match '^([^\s]+\.exe)\b') {
            $ExePath = $Matches[1]
        } else {
            $ExePath = $LocalServerRaw.Trim()
        }
    }

    $CandidateDirs = [System.Collections.Generic.List[string]]::new()
    if ($ExplicitInstallDir) {
        $CandidateDirs.Add($ExplicitInstallDir)
    }
    if ($ExePath -and (Test-Path -LiteralPath $ExePath -PathType Leaf)) {
        $CandidateDirs.Add((Split-Path -Parent $ExePath))
    }
    foreach ($DriveLetter in @("C", "D", "E", "F")) {
        $CandidateDirs.Add("${DriveLetter}:\Program Files\SOLIDWORKS Corp\SOLIDWORKS")
    }

    $InstallDir = ""
    foreach ($Dir in $CandidateDirs) {
        if ($Dir -and (Test-Path -LiteralPath (Join-Path $Dir "SLDWORKS.exe") -PathType Leaf)) {
            $InstallDir = [System.IO.Path]::GetFullPath($Dir)
            if (-not $ExePath) {
                $ExePath = Join-Path $InstallDir "SLDWORKS.exe"
            }
            break
        }
    }

    $InteropDll = if ($InstallDir) {
        Join-Path $InstallDir "api\redist\SolidWorks.Interop.sldworks.dll"
    } else {
        ""
    }
    $InteropAvailable = [bool]($InteropDll -and (Test-Path -LiteralPath $InteropDll -PathType Leaf))
    $Registered = [bool]($Clsid -and $ExePath -and (Test-Path -LiteralPath $ExePath -PathType Leaf))

    if ($Registered) {
        return [pscustomobject]@{
            Registered = $true
            Clsid = $Clsid
            ExePath = $ExePath
            InstallDir = $InstallDir
            InteropAvailable = $InteropAvailable
            Detail = "SldWorks.Application ($Clsid) -> $ExePath"
            RemediationZh = ""
        }
    }
    return [pscustomobject]@{
        Registered = $false
        Clsid = $Clsid
        ExePath = $ExePath
        InstallDir = $InstallDir
        InteropAvailable = $InteropAvailable
        Detail = $(if ($InstallDir) { "SolidWorks files found at $InstallDir, but 64-bit COM ProgID SldWorks.Application is not registered" } else { "SldWorks.Application 64-bit COM registration not found" })
        RemediationZh = "未检测到 64 位 SolidWorks COM 组件注册 (SldWorks.Application)。请确认本机已安装 64 位 SolidWorks，并先手动以管理员或正常方式启动一次 SolidWorks 完成首次组件注册（切勿在 PowerShell 7 中手动调用 GetActiveObject，否则会触发 PlatformNotSupportedException）。"
    }
}

function Find-SolidWorksMsBuild {
    param([string[]]$ExtraCandidates = @())
    if ($env:SOLIDWORKS_MSBUILD_PATH -and (Test-Path -LiteralPath $env:SOLIDWORKS_MSBUILD_PATH -PathType Leaf)) {
        return [System.IO.Path]::GetFullPath($env:SOLIDWORKS_MSBUILD_PATH)
    }
    foreach ($Candidate in $ExtraCandidates) {
        if ($Candidate -and (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
            return [System.IO.Path]::GetFullPath($Candidate)
        }
    }
    $ProgFilesX86 = [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFilesX86)
    $ProgFiles64 = $env:ProgramW6432
    if (-not $ProgFiles64) { $ProgFiles64 = $env:ProgramFiles }

    $VsWhere = if ($ProgFilesX86) { Join-Path $ProgFilesX86 "Microsoft Visual Studio\Installer\vswhere.exe" } else { "" }
    if ($VsWhere -and (Test-Path -LiteralPath $VsWhere -PathType Leaf)) {
        try {
            $Found = & $VsWhere -latest -products * -requires Microsoft.Component.MSBuild -find "MSBuild\**\Bin\amd64\MSBuild.exe" 2>$null | Select-Object -First 1
            if (-not $Found) {
                $Found = & $VsWhere -latest -products * -requires Microsoft.Component.MSBuild -find "MSBuild\**\Bin\MSBuild.exe" 2>$null | Select-Object -First 1
            }
            if ($Found -and (Test-Path -LiteralPath $Found -PathType Leaf)) {
                return [System.IO.Path]::GetFullPath($Found)
            }
        } catch {
            # Continue to well-known paths.
        }
    }

    $Editions = @("Community", "Professional", "Enterprise", "BuildTools")
    foreach ($Root in @($ProgFiles64, $ProgFilesX86)) {
        if (-not $Root) { continue }
        foreach ($Year in @("2022", "2019")) {
            foreach ($Edition in $Editions) {
                foreach ($Sub in @("MSBuild\Current\Bin\amd64\MSBuild.exe", "MSBuild\Current\Bin\MSBuild.exe", "MSBuild\15.0\Bin\amd64\MSBuild.exe")) {
                    $PathCandidate = Join-Path $Root "Microsoft Visual Studio\$Year\$Edition\$Sub"
                    if (Test-Path -LiteralPath $PathCandidate -PathType Leaf) {
                        return [System.IO.Path]::GetFullPath($PathCandidate)
                    }
                }
            }
        }
    }

    $Cmd = Get-Command MSBuild.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1
    if ($Cmd -and ($Cmd -notmatch 'Framework(64)?\\v[24]\.') -and (Test-Path -LiteralPath $Cmd -PathType Leaf)) {
        return [System.IO.Path]::GetFullPath($Cmd)
    }
    return $null
}

function Invoke-SolidWorksExecutionBuild {
    param(
        [Parameter(Mandatory)][string]$CsprojPath,
        [string]$MsBuildPath = "",
        [string]$SolidWorksInstallDir = ""
    )
    $ResolvedMsBuild = if ($MsBuildPath) { $MsBuildPath } else { Find-SolidWorksMsBuild }
    if (-not $ResolvedMsBuild -or -not (Test-Path -LiteralPath $ResolvedMsBuild -PathType Leaf)) {
        return [pscustomobject]@{
            Ok = $false
            ExitCode = 127
            MsBuild = ""
            OutputExe = ""
            Output = "MSBuild.exe not found"
        }
    }
    $ProjectDir = Split-Path -Parent ([System.IO.Path]::GetFullPath($CsprojPath))
    $OutputExe = Join-Path $ProjectDir "bin\Debug\SolidworksExecution.exe"
    $BuildArgs = @(
        $CsprojPath,
        "/t:Build",
        "/p:Configuration=Debug",
        "/p:Platform=AnyCPU",
        "/p:PlatformTarget=x64",
        "/p:Prefer32Bit=false",
        "/p:OutputPath=bin\Debug\",
        "/nologo",
        "/v:minimal"
    )
    if ($SolidWorksInstallDir) {
        $BuildArgs += "/p:SolidWorksInstallDir=$SolidWorksInstallDir"
    }
    $BuildText = & $ResolvedMsBuild @BuildArgs 2>&1 | Out-String
    $ExitCode = $LASTEXITCODE
    $BuiltOk = ($ExitCode -eq 0) -and (Test-Path -LiteralPath $OutputExe -PathType Leaf)
    return [pscustomobject]@{
        Ok = $BuiltOk
        ExitCode = $ExitCode
        MsBuild = $ResolvedMsBuild
        OutputExe = $OutputExe
        Output = $BuildText.Trim()
    }
}

function Get-SolidWorksWorkspaceConfigPath {
    param([string]$DshHome = "$HOME\.dsh")
    return Join-Path ([System.IO.Path]::GetFullPath($DshHome)) "solidworks-workspace.json"
}

function Get-SavedSolidWorksWorkspaceDir {
    param([string]$DshHome = "$HOME\.dsh")
    $ConfigFile = Get-SolidWorksWorkspaceConfigPath -DshHome $DshHome
    if (-not (Test-Path -LiteralPath $ConfigFile -PathType Leaf)) {
        return ""
    }
    try {
        $Parsed = Get-Content -Raw -LiteralPath $ConfigFile -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        if ($Parsed.workspaceDir) {
            return [System.IO.Path]::GetFullPath([string]$Parsed.workspaceDir)
        }
    } catch {
        return ""
    }
    return ""
}

function Get-RecommendedWorkspaceDir {
    param(
        [string]$DshHome = "$HOME\.dsh",
        [string]$ExplicitDir = ""
    )
    if ($ExplicitDir -and $ExplicitDir.Trim() -ne "") {
        return [System.IO.Path]::GetFullPath($ExplicitDir.Trim())
    }
    $Saved = Get-SavedSolidWorksWorkspaceDir -DshHome $DshHome
    if ($Saved) {
        return $Saved
    }
    $SystemDrive = if ($env:SystemDrive) { $env:SystemDrive.TrimEnd('\', '/') } else { "C:" }
    try {
        $NonSystemDrives = @(
            [System.IO.DriveInfo]::GetDrives() | Where-Object {
                $_.DriveType -eq [System.IO.DriveType]::Fixed -and
                $_.IsReady -and
                $_.Name.TrimEnd('\', '/') -ne $SystemDrive
            } | Sort-Object -Property @{ Expression = { if ($_.Name -like "D:*") { 0 } else { 1 } } }, Name
        )
        if ($NonSystemDrives.Count -gt 0) {
            return Join-Path $NonSystemDrives[0].Name "SW-AI-Workspace"
        }
    } catch {
        # Fallback if DriveInfo enumeration is restricted.
    }
    if (Test-Path -LiteralPath "D:\") {
        return "D:\SW-AI-Workspace"
    }
    return Join-Path $HOME "SW-AI-Workspace"
}

function Save-SolidWorksWorkspaceConfig {
    param(
        [Parameter(Mandatory)][string]$DshHome,
        [Parameter(Mandatory)][string]$WorkspaceDir
    )
    $ResolvedHome = [System.IO.Path]::GetFullPath($DshHome)
    $ResolvedWorkspace = [System.IO.Path]::GetFullPath($WorkspaceDir)
    New-Item -ItemType Directory -Force -Path $ResolvedHome, $ResolvedWorkspace | Out-Null
    $ConfigFile = Get-SolidWorksWorkspaceConfigPath -DshHome $ResolvedHome
    $Payload = [ordered]@{
        updatedAtUtc = (Get-Date).ToUniversalTime().ToString("o")
        workspaceDir = $ResolvedWorkspace
    }
    $JsonText = $Payload | ConvertTo-Json -Depth 4
    [System.IO.File]::WriteAllText($ConfigFile, $JsonText, (New-Object System.Text.UTF8Encoding($false)))
    return $ConfigFile
}

function Test-KnownTextOnlyModelId {
    param([string]$ModelId = "")
    if (-not $ModelId -or $ModelId.Trim() -eq "") {
        return $false
    }
    return $ModelId.Trim() -match '(?i)(?:^|[/:-])deepseek(?![^/]*?(?:vl|vision|janus))(?:$|[-._/0-9a-z])'
}

function Get-ConfiguredDefaultModelQuick {
    param(
        [Parameter(Mandatory)][string]$DshHome,
        [Parameter(Mandatory)][string]$DshRoot
    )
    $NodeCmd = Get-Command node -ErrorAction SilentlyContinue
    $PatchHelper = Join-Path $PSScriptRoot "SolidWorksProfilePatch.mjs"
    $DshPkg = Join-Path $DshRoot "node_modules\@deepseek-ai\dsh\package.json"
    if (-not $NodeCmd -or -not (Test-Path -LiteralPath $PatchHelper) -or -not (Test-Path -LiteralPath $DshPkg)) {
        return $null
    }
    try {
        $RawJson = & node $PatchHelper --mode inspect-default-model --dsh-root $DshRoot --dsh-home $DshHome 2>$null | Out-String
        if ($LASTEXITCODE -ne 0 -or -not $RawJson.Trim()) {
            return $null
        }
        return ($RawJson.Trim() | ConvertFrom-Json)
    } catch {
        return $null
    }
}
