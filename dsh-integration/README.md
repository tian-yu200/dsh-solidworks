# SolidWorks DSH Integration

This integration adds the SolidWorks MCP tool surface to both DeepSeek Harness
browser profiles and supplies a dedicated CAD preset and policy.

## Architecture

```text
DSH web or solidworks profile
  -> host-owned MCP client and nine high-level tools
  -> solidworks-cad preset for CAD instructions and skills
  -> Python orchestration and job policy
  -> SolidPilot compiler and C# execution service
  -> SolidWorks COM
```

The model never receives direct access to COM, Shell, macros, REST execution
endpoints, or the legacy low-level CAD MCP tool surface.

Both browser profiles own an MCP client so every resumed session sees the nine
tools from the host layer, including sessions composed from an older preset
generation. The `solidworks-cad` preset owns only CAD instructions and skills.
The dedicated profile also selects that preset by default and adds the policy
plugin.

## Installed Surfaces

- Profile: `%USERPROFILE%\.dsh\profiles\solidworks`
- Preset: `%USERPROFILE%\.dsh\.agent-presets\solidworks-cad`
- Policy plugin: `%USERPROFILE%\.dsh\plugins\solidworks-policy`
- MCP adapter: `mcp-server-solidworks-main\adapters\dsh\server.py`
- Job state: `<repository>\state\jobs`
- Backups: `<repository>\backups`

## Nine-Tool Surface

The dedicated MCP exposes exactly:

1. `start_job`
2. `inspect_state`
3. `submit_feature_graph`
4. `apply_document_edits`
5. `drawing_workflow`
6. `save_or_export`
7. `request_confirmation`
8. `confirm_action`
9. `finish_job`

The policy plugin denies other `mcp__solidworks__*` calls.

## Safety Model

- `start_job` returns `job_id` and a private `job_token`.
- The active document identity is checked before each job-scoped call.
- Existing source files receive a first-write backup when possible.
- Feature Graph, document edit, and drawing mutations use SolidWorks undo
  transactions with post-rollback verification.
- Destructive and batch actions pause as `pending_confirmation`.
- `request_confirmation` rotates a one-time token and returns the exact summary
  the user must approve.
- `confirm_action` accepts only the unchanged action, token, confirmation text,
  and summary.
- `finish_job` performs final objective verification.

## Install

```powershell
& .\dsh-integration\scripts\Install-SolidWorksDsh.ps1 -DshRoot C:\path\to\deepseek-harness
```

The installer backs up affected DSH configuration to the repository's
`config-backups\<timestamp>` directory. It does not modify provider
settings, API keys, or permission presets.

## Start

The ordinary browser process exposes the MCP tools to existing and new sessions.
Select the `solidworks-cad` preset for the CAD workflow instructions:

```powershell
C:\path\to\deepseek-harness\node_modules\.bin\dsh.CMD web
```

The dedicated launcher selects that preset by default:

```powershell
& .\dsh-integration\scripts\Start-SolidWorksDsh.ps1 -DshRoot C:\path\to\deepseek-harness
```

Equivalent direct launch:

```powershell
C:\path\to\deepseek-harness\node_modules\.bin\dsh.CMD --profile solidworks
```

## Diagnose

```powershell
& .\dsh-integration\scripts\Doctor-SolidWorksDsh.ps1 -DshRoot C:\path\to\deepseek-harness
& .\dsh-integration\scripts\Export-SolidWorksDshConfig.ps1 -DshRoot C:\path\to\deepseek-harness
```

The exported config is redacted for common key, authorization, bearer, and token
forms. Review it before publishing.

## Restore

```powershell
& .\dsh-integration\scripts\Restore-SolidWorksDsh.ps1 `
  -BackupRoot .\config-backups\<timestamp>
```

## Operational Limits

- An unsaved document has no source file to back up.
- Undo is the transaction mechanism, but cross-document and external-file actions
  still have recovery limits.
- `rollback_failed` means the active document may remain partially modified.
- MCP cannot cryptographically prove a human supplied approval. The preset and
  skill require a fresh explicit user message before using the one-time token.
- A user document switch invalidates the job target and blocks the next write.
