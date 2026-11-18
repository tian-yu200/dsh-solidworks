import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const scriptsDir = fileURLToPath(new URL('..', import.meta.url));
const repoRoot = path.resolve(scriptsDir, '..', '..');
const envHelpersPath = path.join(scriptsDir, 'SolidWorksEnvHelpers.ps1');
const startScriptPath = path.join(scriptsDir, 'Start-SolidWorksDsh.ps1');
const setupScriptPath = path.join(scriptsDir, 'Setup-SolidWorksEasy.ps1');
const doctorScriptPath = path.join(scriptsDir, 'Doctor-SolidWorksDsh.ps1');

function runPwsh(args, envOverrides = {}) {
  // Set UTF-8 output encoding inside the child PowerShell so captured stdout decodes cleanly in Node.
  const result = spawnSync(
    'powershell.exe',
    ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', ...args],
    {
      encoding: 'utf8',
      env: { ...process.env, ...envOverrides },
      timeout: 60000,
    },
  );
  return {
    status: result.status,
    stdout: result.stdout ?? '',
    stderr: result.stderr ?? '',
    output: (result.stdout ?? '') + (result.stderr ?? ''),
  };
}

function buildMinimalX64PeBuffer() {
  // Minimal 512-byte valid PE32+ (x64) header: MZ @ 0, e_lfanew = 0x80, 'PE\0\0', Machine = 0x8664, Magic = 0x020B
  const buf = Buffer.alloc(512, 0);
  buf[0] = 0x4d; // 'M'
  buf[1] = 0x5a; // 'Z'
  buf.writeInt32LE(0x80, 0x3c);
  buf.write('PE\0\0', 0x80, 'ascii');
  buf.writeUInt16LE(0x8664, 0x84); // IMAGE_FILE_MACHINE_AMD64
  buf.writeUInt16LE(1, 0x86); // NumberOfSections
  buf.writeUInt16LE(0xf0, 0x94); // SizeOfOptionalHeader
  buf.writeUInt16LE(0x020b, 0x98); // PE32+ Magic
  return buf;
}

function createFakeDshRoot(rootDir) {
  const dshRoot = path.join(rootDir, 'fake-dsh-root');
  const binDir = path.join(dshRoot, 'node_modules', '.bin');
  const pkgDir = path.join(dshRoot, 'node_modules', '@deepseek-ai', 'dsh');
  const yamlDir = path.join(dshRoot, 'node_modules', 'js-yaml');
  fs.mkdirSync(binDir, { recursive: true });
  fs.mkdirSync(pkgDir, { recursive: true });
  fs.mkdirSync(yamlDir, { recursive: true });

  fs.writeFileSync(
    path.join(pkgDir, 'package.json'),
    JSON.stringify({ name: '@deepseek-ai/dsh', version: '0.1.1-test' }),
  );
  // Minimal JSON-compatible js-yaml stub for loadDshYaml in isolated tests
  fs.writeFileSync(path.join(yamlDir, 'package.json'), JSON.stringify({ name: 'js-yaml', main: 'index.js' }));
  fs.writeFileSync(
    path.join(yamlDir, 'index.js'),
    `
    class Type { constructor(tag, opts) { this.tag = tag; this.opts = opts; } }
    const DEFAULT_SCHEMA = { extend() { return this; } };
    function load(src) { return src && src.trim() ? JSON.parse(src) : []; }
    function dump(obj) { return JSON.stringify(obj, null, 2); }
    module.exports = { Type, DEFAULT_SCHEMA, load, dump };
    `,
  );

  // Fake dsh.CMD that echoes JSON facts (or clean config dump on --dump-config)
  const probeScript = path.join(binDir, 'dsh-probe.js');
  fs.writeFileSync(
    probeScript,
    `
    const args = process.argv.slice(2);
    if (args.includes('--dump-config')) {
      process.stdout.write(JSON.stringify([
        { id: 'llm-pi-ai', config: { providers: { gpt: { models: [{ id: 'gpt-6.1-sol', input: ['text', 'image'] }] } } } },
        { id: 'agent-default-model', config: { provider: 'gpt', model: 'gpt-6.1-sol' } },
        { id: 'solidworks-policy', name: '@dsh-solidworks/policy' }
      ]));
      process.exit(0);
    }
    process.stdout.write('DSH_LAUNCH_FACTS:' + JSON.stringify({
      cwd: process.cwd(),
      permissionMode: process.env.DSH_PERMISSION_MODE || '',
      args
    }) + '\\n');
    `,
  );
  fs.writeFileSync(
    path.join(binDir, 'dsh.CMD'),
    `@echo off\r\n"${process.execPath}" "${probeScript}" %*\r\n`,
  );
  return dshRoot;
}

test('all PowerShell onboarding and diagnostic scripts carry UTF-8 BOM and parse cleanly', () => {
  for (const scriptName of [
    'SolidWorksEnvHelpers.ps1',
    'Doctor-SolidWorksDsh.ps1',
    'Start-SolidWorksDsh.ps1',
    'Setup-SolidWorksEasy.ps1',
  ]) {
    const fullPath = path.join(scriptsDir, scriptName);
    const bytes = fs.readFileSync(fullPath);
    assert.equal(bytes[0], 0xef, `${scriptName} must start with UTF-8 BOM (EF BB BF) for Windows PowerShell 5.1 CP936 safety`);
    assert.equal(bytes[1], 0xbb);
    assert.equal(bytes[2], 0xbf);
  }

  const res = runPwsh([
    '-Command',
    `
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    foreach ($f in @('SolidWorksEnvHelpers.ps1','Doctor-SolidWorksDsh.ps1','Start-SolidWorksDsh.ps1','Setup-SolidWorksEasy.ps1')) {
      $p = Join-Path '${scriptsDir.replace(/'/g, "''")}' $f
      $t = $null; $e = $null
      [void][System.Management.Automation.Language.Parser]::ParseFile($p, [ref]$t, [ref]$e)
      if ($e.Count -gt 0) { throw "$f has parse errors: $($e -join '; ')" }
    }
    Write-Output "PARSE_OK"
    `,
  ]);
  assert.equal(res.status, 0, res.output);
  assert.match(res.output, /PARSE_OK/);
});

test('SolidWorksEnvHelpers diagnoses 64-bit, .NET 4.8, PE headers, and non-C workspace persistence', t => {
  const tempRoot = fs.realpathSync.native(fs.mkdtempSync(path.join(os.tmpdir()), 'sw-env-helpers-'));
  t.after(() => fs.rmSync(tempRoot, { recursive: true, force: true }));

  const fakeX64Exe = path.join(tempRoot, 'Fake64.exe');
  fs.writeFileSync(fakeX64Exe, buildMinimalX64PeBuffer());
  const dshHome = path.join(tempRoot, 'dsh-home');
  const customWs = path.join(tempRoot, 'D-Drive-Sim', 'SW-AI-Workspace');

  const res = runPwsh([
    '-Command',
    `
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    . '${envHelpersPath.replace(/'/g, "''")}'
    $wow = Test-SolidWorks64BitEnvironment -Is64BitOS $true -Is64BitProcess $false
    $netOld = Get-DotNetFramework48Status -OverrideRelease 461808
    $netOk = Get-DotNetFramework48Status -OverrideRelease 533509
    $pe64 = Get-ExecutablePeArchitecture -ExePath '${fakeX64Exe.replace(/'/g, "''")}'
    $peMissing = Get-ExecutablePeArchitecture -ExePath '${path.join(tempRoot, 'none.exe').replace(/'/g, "''")}'
    [void](Save-SolidWorksWorkspaceConfig -DshHome '${dshHome.replace(/'/g, "''")}' -WorkspaceDir '${customWs.replace(/'/g, "''")}')
    $savedWs = Get-SavedSolidWorksWorkspaceDir -DshHome '${dshHome.replace(/'/g, "''")}'
    $rec = Get-RecommendedWorkspaceDir -DshHome '${path.join(tempRoot, 'fresh-home').replace(/'/g, "''")}'
    [pscustomobject]@{
      wowOk = $wow.Ok
      wowMsg = $wow.RemediationZh
      netOldOk = $netOld.Ok
      netOk = $netOk.Ok
      pe64Arch = $pe64.Architecture
      peMissingArch = $peMissing.Architecture
      savedWs = $savedWs
      rec = $rec
      blockFlash = (Test-KnownTextOnlyModelId 'azure-sub2/deepseek-v4.1-flash')
      allowGpt = (Test-KnownTextOnlyModelId 'copilot/gpt-6.1-sol')
      allowVl = (Test-KnownTextOnlyModelId 'deepseek-vl2')
    } | ConvertTo-Json -Compress
    `,
  ]);
  assert.equal(res.status, 0, res.output);
  const data = JSON.parse(res.stdout.trim());
  assert.equal(data.wowOk, false);
  assert.match(data.wowMsg, /PlatformNotSupportedException/);
  assert.equal(data.netOldOk, false);
  assert.equal(data.netOk, true);
  assert.equal(data.pe64Arch, 'x64');
  assert.equal(data.peMissingArch, 'missing');
  assert.equal(data.savedWs, customWs);
  assert.ok(data.rec.endsWith('SW-AI-Workspace'));
  assert.equal(data.blockFlash, true);
  assert.equal(data.allowGpt, false);
  assert.equal(data.allowVl, false);
});

test('Start-SolidWorksDsh clears danger-full-access env, switches to non-C workspace, and blocks text-only Flash models', t => {
  const tempRoot = fs.realpathSync.native(fs.mkdtempSync(path.join(os.tmpdir()), 'sw-start-test-'));
  t.after(() => fs.rmSync(tempRoot, { recursive: true, force: true }));

  const dshRoot = createFakeDshRoot(tempRoot);
  const dshHome = path.join(tempRoot, 'dsh-home');
  const workspaceDir = path.join(tempRoot, 'CAD-Workspace');
  fs.mkdirSync(path.join(dshHome, 'profiles', 'solidworks'), { recursive: true });

  // 1. Clean passthrough for --dump-config (no banner noise)
  const dumpRes = runPwsh([
    '-Command',
    `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; & '${startScriptPath.replace(/'/g, "''")}' -DshRoot '${dshRoot.replace(/'/g, "''")}' --dump-config`,
  ]);
  assert.equal(dumpRes.status, 0, dumpRes.output);
  assert.doesNotMatch(dumpRes.stdout, /正在启动/);
  assert.equal(JSON.parse(dumpRes.stdout.trim())[0].id, 'llm-pi-ai');

  // 2. Blocks text-only DeepSeek-V4.1-Flash model at startup with clear Chinese alert
  fs.writeFileSync(
    path.join(dshHome, 'settings.yaml'),
    JSON.stringify({ 'agent-default-model': { provider: 'magpie-responses', model: 'DeepSeek-V4.1-Flash' } }),
  );
  const blockedRes = runPwsh([
    '-Command',
    `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; & '${startScriptPath.replace(/'/g, "''")}' -DshRoot '${dshRoot.replace(/'/g, "''")}' -DshHome '${dshHome.replace(/'/g, "''")}'`,
  ]);
  assert.equal(blockedRes.status, 1, blockedRes.output);
  assert.match(blockedRes.output, /看图建模拦截提示/);
  assert.match(blockedRes.output, /DeepSeek-V4\.1-Flash/);

  // 3. Valid vision model launches in WorkspaceDir and neutralizes DSH_PERMISSION_MODE=danger-full-access
  fs.writeFileSync(
    path.join(dshHome, 'settings.yaml'),
    JSON.stringify({ 'agent-default-model': { provider: 'gpt', model: 'gpt-6.1-sol' } }),
  );
  const okRes = runPwsh(
    [
      '-Command',
      `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; & '${startScriptPath.replace(/'/g, "''")}' -DshRoot '${dshRoot.replace(/'/g, "''")}' -DshHome '${dshHome.replace(/'/g, "''")}' -WorkspaceDir '${workspaceDir.replace(/'/g, "''")}' --no-open`,
    ],
    { DSH_PERMISSION_MODE: 'danger-full-access' },
  );
  assert.equal(okRes.status, 0, okRes.output);
  assert.match(okRes.output, /AI 推理在云端运行/);
  assert.match(okRes.output, /不是报错/);
  const factsLine = okRes.stdout.split(/\r?\n/).find(line => line.startsWith('DSH_LAUNCH_FACTS:'));
  assert.ok(factsLine, okRes.output);
  const facts = JSON.parse(factsLine.slice('DSH_LAUNCH_FACTS:'.length));
  assert.equal(facts.permissionMode, 'workspace-write');
  assert.equal(facts.cwd.toLowerCase(), workspaceDir.toLowerCase());
  assert.deepEqual(facts.args, ['--profile', 'solidworks', '--no-open']);
});

test('Setup-SolidWorksEasy guides zero-CLI mechanical users, auto-builds x64 exe via MSBuild, and guards vision models', t => {
  const tempRoot = fs.realpathSync.native(fs.mkdtempSync(path.join(os.tmpdir()), 'sw-easy-wizard-'));
  t.after(() => fs.rmSync(tempRoot, { recursive: true, force: true }));

  const dshRoot = createFakeDshRoot(tempRoot);
  const dshHome = path.join(tempRoot, 'dsh-home');
  const workspaceDir = path.join(tempRoot, 'NonC-Workspace');
  const fakeProject = path.join(tempRoot, 'project');
  const fakePython = path.join(tempRoot, 'fake-python.cmd');
  fs.writeFileSync(fakePython, '@echo off\r\nexit /b 0\r\n');

  // Create minimal isolated project skeleton so no file in repoRoot is written
  const execProjDir = path.join(fakeProject, 'mcp-server-solidworks-main', 'execution', 'solidworks', 'SolidworksExecution');
  fs.mkdirSync(execProjDir, { recursive: true });
  fs.writeFileSync(path.join(execProjDir, 'SolidworksExecution.csproj'), '<Project />');

  // Create fake MSBuild.cmd that verifies x64 / Prefer32Bit=false flags and produces an x64 PE32+ SolidworksExecution.exe
  const fakeMsBuild = path.join(tempRoot, 'FakeMSBuild.cmd');
  const msbuildNodeHelper = path.join(tempRoot, 'msbuild-helper.js');
  const builtExePath = path.join(execProjDir, 'bin', 'Debug', 'SolidworksExecution.exe');
  const x64Hex = buildMinimalX64PeBuffer().toString('hex');
  fs.writeFileSync(
    msbuildNodeHelper,
    `
    const fs = require('fs');
    const path = require('path');
    const args = process.argv.slice(2).join(' ');
    if (!args.includes('/p:PlatformTarget=x64') || !args.includes('/p:Prefer32Bit=false')) {
      console.error('Missing x64 build flags: ' + args);
      process.exit(2);
    }
    const out = ${JSON.stringify(builtExePath)};
    fs.mkdirSync(path.dirname(out), { recursive: true });
    fs.writeFileSync(out, Buffer.from('${x64Hex}', 'hex'));
    `,
  );
  fs.writeFileSync(fakeMsBuild, `@echo off\r\n"${process.execPath}" "${msbuildNodeHelper}" %*\r\n`);

  // 1. Pre-flight blocks 32-bit WOW64 terminal with Chinese PlatformNotSupportedException explanation
  const wowFail = runPwsh([
    '-Command',
    `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; & '${setupScriptPath.replace(/'/g, "''")}' -DshHome '${dshHome.replace(/'/g, "''")}' -DshRoot '${dshRoot.replace(/'/g, "''")}' -ProjectRoot '${fakeProject.replace(/'/g, "''")}' -Python '${fakePython.replace(/'/g, "''")}' -NonInteractive -SkipInstall -SkipDoctor -TestOverrideArchMode wow64`,
  ]);
  assert.equal(wowFail.status, 1, wowFail.output);
  assert.match(wowFail.output, /AI 算力在云端运行/);
  assert.match(wowFail.output, /PlatformNotSupportedException/);

  // 2. Pre-flight blocks missing .NET Framework 4.8
  const netFail = runPwsh([
    '-Command',
    `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; & '${setupScriptPath.replace(/'/g, "''")}' -DshHome '${dshHome.replace(/'/g, "''")}' -DshRoot '${dshRoot.replace(/'/g, "''")}' -ProjectRoot '${fakeProject.replace(/'/g, "''")}' -Python '${fakePython.replace(/'/g, "''")}' -NonInteractive -SkipInstall -SkipDoctor -TestOverrideDotNetRelease 461808`,
  ]);
  assert.equal(netFail.status, 1, netFail.output);
  assert.match(netFail.output, /\.NET Framework 4\.8/);

  // 3. Missing SolidworksExecution.exe triggers automatic x64 MSBuild compilation and persists workspace
  assert.equal(fs.existsSync(builtExePath), false);
  const autoBuildOk = runPwsh(
    [
      '-Command',
      `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; & '${setupScriptPath.replace(/'/g, "''")}' -DshHome '${dshHome.replace(/'/g, "''")}' -DshRoot '${dshRoot.replace(/'/g, "''")}' -ProjectRoot '${fakeProject.replace(/'/g, "''")}' -Python '${fakePython.replace(/'/g, "''")}' -WorkspaceDir '${workspaceDir.replace(/'/g, "''")}' -NonInteractive -SkipInstall -SkipDoctor`,
    ],
    { SOLIDWORKS_MSBUILD_PATH: fakeMsBuild },
  );
  assert.equal(autoBuildOk.status, 0, autoBuildOk.output);
  assert.equal(fs.existsSync(builtExePath), true, 'MSBuild should have compiled SolidworksExecution.exe');
  assert.match(autoBuildOk.output, /已自动编译生成 64位 SolidworksExecution\.exe \(x64\)/);
  assert.match(autoBuildOk.output, /恭喜！SolidWorks AI 看图建模环境已全部配置就绪/);
  const wsConfig = JSON.parse(fs.readFileSync(path.join(dshHome, 'solidworks-workspace.json'), 'utf8'));
  assert.equal(wsConfig.workspaceDir.toLowerCase(), workspaceDir.toLowerCase());

  // 4. Doctor step in Setup-SolidWorksEasy intercepts DeepSeek-V4.1-Flash with unmistakable Chinese alert
  fs.mkdirSync(path.join(dshHome, 'profiles', 'solidworks'), { recursive: true });
  fs.writeFileSync(
    path.join(dshHome, 'settings.yaml'),
    JSON.stringify({ 'agent-default-model': { provider: 'magpie-responses', model: 'azure-sub2/deepseek-v4.1-flash' } }),
  );
  const dumpFile = path.join(tempRoot, 'dump.txt');
  const flashBlocked = runPwsh([
    '-Command',
    `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; & '${setupScriptPath.replace(/'/g, "''")}' -DshHome '${dshHome.replace(/'/g, "''")}' -DshRoot '${dshRoot.replace(/'/g, "''")}' -ProjectRoot '${fakeProject.replace(/'/g, "''")}' -Python '${fakePython.replace(/'/g, "''")}' -WorkspaceDir '${workspaceDir.replace(/'/g, "''")}' -DumpPath '${dumpFile.replace(/'/g, "''")}' -NonInteractive -SkipInstall -SkipDump`,
  ]);
  assert.equal(flashBlocked.status, 1, flashBlocked.output);
  assert.match(flashBlocked.output, /重要告警：当前默认模型无法看图建模/);
  assert.match(flashBlocked.output, /deepseek-v4\.1-flash/i);
});

test('Doctor-SolidWorksDsh reports new 64-bit, .NET 4.8, PE binary, and permission safety checks', t => {
  const tempRoot = fs.realpathSync.native(fs.mkdtempSync(path.join(os.tmpdir()), 'sw-doctor-checks-'));
  t.after(() => fs.rmSync(tempRoot, { recursive: true, force: true }));

  const dshRoot = createFakeDshRoot(tempRoot);
  const dshHome = path.join(tempRoot, 'dsh-home');
  const fakeProject = path.join(tempRoot, 'doctor-project');
  const fakeExe = path.join(
    fakeProject,
    'mcp-server-solidworks-main',
    'execution',
    'solidworks',
    'SolidworksExecution',
    'bin',
    'Debug',
    'SolidworksExecution.exe',
  );
  fs.mkdirSync(path.dirname(fakeExe), { recursive: true });
  fs.writeFileSync(fakeExe, buildMinimalX64PeBuffer());
  const dumpFile = path.join(tempRoot, 'profile.dump.txt');
  fs.mkdirSync(path.join(dshHome, 'profiles', 'solidworks'), { recursive: true });
  // Simulate a profile with danger-full-access to verify Permission preset safety flags it
  fs.writeFileSync(
    path.join(dshHome, 'profiles', 'solidworks', 'cordis.patch.yml'),
    '  defaultPreset: danger-full-access\n',
  );

  const docRes = runPwsh([
    '-Command',
    `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; & '${doctorScriptPath.replace(/'/g, "''")}' -DshHome '${dshHome.replace(/'/g, "''")}' -DshRoot '${dshRoot.replace(/'/g, "''")}' -ProjectRoot '${fakeProject.replace(/'/g, "''")}' -DumpPath '${dumpFile.replace(/'/g, "''")}' -SkipDump`,
  ]);
  assert.equal(docRes.status, 1, docRes.output);
  assert.match(docRes.output, /64-bit environment\s+True/);
  assert.match(docRes.output, /\.NET Framework 4\.8\s+True/);
  assert.match(docRes.output, /Execution binary \(PE\/CLR\)\s+True/);
  assert.match(docRes.output, /Permission preset safety\s+False/);
  assert.match(docRes.output, /权限模式告警/);
  assert.match(docRes.output, /SolidWorks AI 环境中文诊断摘要/);
});
