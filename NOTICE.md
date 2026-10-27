# Notices

This repository includes and modifies SolidPilot from:
https://github.com/eyfel/mcp-server-solidworks

SolidPilot copyright:
Copyright (c) 2025-2026 Çağatay Bakan.

SolidPilot and this derivative distribution are licensed under the GNU Affero
General Public License version 3. See `LICENSE`.

The DeepSeek Harness integration, DSH profile, policy plugin, orchestration
changes, and related documentation were modified in 2026 by contributors to
`tian-yu200/dsh-solidworks`.

SolidWorks is a trademark of Dassault Systemes SolidWorks Corporation. This
project is independent and is not affiliated with or endorsed by Dassault
Systemes, DeepSeek, or the upstream SolidPilot author.

## Cosmetic thread reference

The `IFeatureManager.InsertCosmeticThread3` call shape (seven positional
arguments, `ISO = 8`, `"Tapped Hole"`, blind `0` / through `2`) and the
edge-selection pattern that precedes it (clear the selection, then pre-select
the circular bore-mouth edge with mark 0 and `append = False`) used in
`mcp-server-solidworks-main/execution/solidworks/SolidworksExecution/Services/SolidWorksService.Threads.cs`
are adapted from:

https://github.com/wzyn20051216/solidworks-automation-skill

MIT License, Copyright (c) 2026 SolidWorks Automation Skill Contributors.

The API facts themselves are Dassault Systemes' (SOLIDWORKS API Help) and were
re-verified against the SolidWorks Interop assemblies; the MIT-licensed
reference supplied the call shape and the selection order.
