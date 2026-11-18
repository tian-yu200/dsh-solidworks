"""Contract and build-smoke tests for SolidworksExecution x64 architecture,
PlatformNotSupportedException guards, and EditFeature(rename) verification.
"""
import os
import shutil
import struct
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXEC_DIR = os.path.join(REPO_ROOT, "execution", "solidworks")
PROJ_DIR = os.path.join(EXEC_DIR, "SolidworksExecution")
CSPROJ = os.path.join(PROJ_DIR, "SolidworksExecution.csproj")
SLN = os.path.join(EXEC_DIR, "SolidworksExecution.sln")
PROGRAM_CS = os.path.join(PROJ_DIR, "Program.cs")
SERVICE_CS = os.path.join(PROJ_DIR, "Services", "SolidWorksService.cs")
FEATURES_CS = os.path.join(PROJ_DIR, "Services", "SolidWorksService.Features.cs")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_csproj_disables_prefer32bit_and_defines_x64_configurations():
    """SolidWorks is 64-bit only; AnyCPU without Prefer32Bit=false compiles as 32-bit WOW64."""
    csproj = _read(CSPROJ)
    sln = _read(SLN)
    assert "<Prefer32Bit>false</Prefer32Bit>" in csproj
    assert csproj.count("<Prefer32Bit>false</Prefer32Bit>") >= 3
    assert "<Prefer32Bit>true</Prefer32Bit>" not in csproj
    assert "'$(Configuration)|$(Platform)' == 'Debug|x64'" in csproj
    assert "'$(Configuration)|$(Platform)' == 'Release|x64'" in csproj
    assert "<PlatformTarget>x64</PlatformTarget>" in csproj
    assert "Debug|x64 = Debug|x64" in sln
    assert "Release|x64 = Release|x64" in sln


def test_program_and_service_guard_platform_not_supported_exception():
    program = _read(PROGRAM_CS)
    service = _read(SERVICE_CS)
    assert "HttpListener.IsSupported" in program
    assert "catch (PlatformNotSupportedException" in program
    assert "Is64BitProcess" in program

    # SolidWorks.Interop.sldworks defines an `Environment` interface; System.Environment must be qualified.
    assert "System.Environment.Is64BitProcess" in service
    assert "catch (PlatformNotSupportedException" in service
    assert "catch (InvalidCastException" in service


def test_edit_feature_verifies_rename_readback_and_walks_subfeatures():
    features = _read(FEATURES_CS)
    edit_idx = features.index("public ExecutionResponse EditFeature(")
    edit_body = features[edit_idx:]
    assert "WalkFeatureTree(modelDoc)" in edit_body
    assert "FEATURE_RENAME_REJECTED" in edit_body
    assert "actualName = feature.Name" in edit_body


def _has_local_csharp_build_prereqs():
    if os.name != "nt" or shutil.which("dotnet") is None:
        return False
    sw_candidates = [
        r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\api\redist\SolidWorks.Interop.sldworks.dll",
        r"D:\solidworks\SOLIDWORKS\api\redist\SolidWorks.Interop.sldworks.dll",
    ]
    has_interop = any(os.path.isfile(p) for p in sw_candidates)
    has_netfx = (
        os.path.isfile(r"C:\Program Files (x86)\Reference Assemblies\Microsoft\Framework\.NETFramework\v4.8\mscorlib.dll")
        or os.path.isfile(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\mscorlib.dll")
    )
    return has_interop and has_netfx


@pytest.mark.skipif(
    not _has_local_csharp_build_prereqs(),
    reason="dotnet SDK or SolidWorks Interop / .NET Framework 4.8 assemblies not installed on this host",
)
def test_msbuild_compiles_x64_binary_with_pe32plus_header_and_no_32bit_flags(tmp_path):
    """Verify C# source compiles cleanly to a true PE32+ x64 executable without touching live bin/Debug."""
    out_dir = str(tmp_path / "bin") + os.sep
    obj_dir = str(tmp_path / "obj") + os.sep
    proc = subprocess.run(
        [
            "dotnet",
            "msbuild",
            CSPROJ,
            "/t:Rebuild",
            "/p:Configuration=Debug",
            "/p:Platform=x64",
            f"/p:OutDir={out_dir}",
            f"/p:BaseIntermediateOutputPath={obj_dir}",
            "/v:minimal",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
    )
    assert proc.returncode == 0, f"dotnet msbuild failed:\n{proc.stdout}\n{proc.stderr}"
    exe_path = os.path.join(out_dir, "SolidworksExecution.exe")
    assert os.path.isfile(exe_path)

    data = open(exe_path, "rb").read()
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    machine = struct.unpack_from("<H", data, pe_offset + 4)[0]
    magic = struct.unpack_from("<H", data, pe_offset + 24)[0]
    assert machine == 0x8664, f"expected IMAGE_FILE_MACHINE_AMD64 (0x8664), got 0x{machine:04X}"
    assert magic == 0x020B, f"expected PE32+ magic (0x020B), got 0x{magic:04X}"
