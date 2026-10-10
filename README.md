# dsh-solidworks

这是一个面向 SolidWorks 的 DeepSeek Harness 集成，把自然语言 CAD 任务收敛到受控的高层 MCP 工具、Python 编排器和 SolidPilot 执行层。

项目目前是 Windows-only public alpha。它基于并修改了上游 [SolidPilot](https://github.com/eyfel/mcp-server-solidworks)，保留原作者版权并使用 AGPL-3.0，详见 [NOTICE.md](NOTICE.md) 和 [LICENSE](LICENSE)。

## 技术结论

改造可行。推荐结构不是让模型直接调用 SolidWorks COM，而是把现有三部分组合成分层系统：

```text
DeepSeek Harness
  -> solidworks-cad preset 与 policy
  -> 9 个高层 MCP 工具
  -> Python 编排、权限、任务状态、确认与验证
  -> SolidPilot 编译器和 C# 执行服务
  -> SolidWorks COM
  -> SolidWorks
```

相关目录的职责：

| 目录 | 职责 |
| --- | --- |
| DeepSeek Harness installation | DSH 主程序和浏览器交互界面 |
| upstream SolidPilot subtree | SolidWorks MCP、编译器和执行服务 |
| this repository | 专用 profile、Skill、MCP 编排器和策略改造 |

这种结构保留真实 SolidWorks 后端，同时把模型限制在任务级工具，避免模型直接拼接 COM、Shell、宏或低层 CAD 调用。

## 模型可见工具

专用会话只能看到以下 9 个 `mcp__solidworks__*` 工具：

| 工具 | 用途 |
| --- | --- |
| `start_job` | 启动任务、连接 SolidWorks、锁定目标文档并返回任务凭据 |
| `inspect_state` | 读取状态、文档分析结果和受控 recipe/schema |
| `submit_feature_graph` | 一次提交完整 Feature Graph 创建零件或装配体 |
| `apply_document_edits` | 对当前目标执行严格白名单编辑 |
| `drawing_workflow` | 创建工程图、视图、标注、BOM、气泡、孔表和标准检查 |
| `save_or_export` | 保存、导出、批量导出或关闭 |
| `request_confirmation` | 为待确认危险动作刷新一次性确认数据 |
| `confirm_action` | 在用户明确批准后执行精确待办动作 |
| `finish_job` | 完成最终客观验证并结束任务 |

除以上 9 个 SolidWorks 工具外，策略插件还放行 4 个支撑工具：
`ask_user_question`（向用户提问）、`todo_write`（维护待办清单）、`skill`（加载技能）、
`modlens_read_image`（读取图片证据；仅接受本地图片文件路径，`http://`、`https://`、
`ftp://`、`file://` 等 URL 形式一律拒绝）。除此之外的任何工具一律拒绝。

注册事实源：
`mcp-server-solidworks-main/adapters/dsh/server.py`。

## 关键能力

- 独立 `solidworks` profile，不污染通用 `web` profile。
- 任务级 `job_id` 与私密 `job_token`。
- 活动文档身份锁，用户切换文档后阻止继续写入。
- 现有文件首次写入前备份。
- SolidWorks Undo transaction、失败回滚和回滚后验证。
- Feature Graph 使用现有确定性编译器，长度为米，角度为弧度。
- 现有文档编辑严格白名单。
- 工程图支持标准视图、展开图、剖视图、局部视图、模型标注、自动尺寸、中心标记、BOM、气泡、孔表和标准检查。
- 覆盖、批量、删除、关闭全部或丢弃未保存修改等风险动作需要精确确认。
- 每次写入后检查文档身份、重建状态和适用的几何/工程图结果。
- 输出返回 `execution_status`、`verification_status`、`recovery_status` 与 `trust_level`。

## 工作流

1. 使用 `inspect_state(scope="state")` 检查 SolidWorks 与活动文档。
2. 使用 `inspect_state(scope="reference")` 读取本任务需要的 recipe 或 Feature Graph schema。大参考按页返回（每页至多 40 行、约 4000 字符；`reference_limit` 传更小值有效，更大值被钳制，`0` 表示默认），用 `reference_offset`/`reference_limit` 翻页并跟随 `page.has_more`/`page.next_offset`；`feature_graph_schema` 的 `page.index.feature_types` 列出节点类型，`page.index.sections[].line` 可直接跳转到某节。
3. 使用 `start_job` 获取 `job_id` 和私密 `job_token`。
4. 根据任务调用 `submit_feature_graph`、`apply_document_edits` 或 `drawing_workflow`。
5. 收到 `pending_confirmation` 时调用 `request_confirmation`，向用户展示精确 `action_summary`（其中会点名目标文档的路径或标题），等待新消息明确批准后再调用 `confirm_action`。
6. 使用 `inspect_state` 检查结果，按需调用 `save_or_export`。
7. 最后调用 `finish_job`。`verification_failed`、`partial` 或 `rollback_failed` 都不能作为成功交付。

完整代理规则：
[`dsh-integration/presets/solidworks-cad/skills/solidworks-cad/SKILL.md`](dsh-integration/presets/solidworks-cad/skills/solidworks-cad/SKILL.md)

工具边界说明：
[`docs/SolidWorks-MCP工具清单.md`](docs/SolidWorks-MCP工具清单.md)

模型图片输入配置（换模型时保持看图能力）：
[`docs/DSH模型图片输入配置.md`](docs/DSH模型图片输入配置.md)

## 目录

```text
dsh-solidworks
├─ dsh-integration\
│  ├─ profiles\solidworks\
│  ├─ presets\solidworks-cad\
│  ├─ plugins\solidworks-policy\
│  └─ scripts\
├─ .dsh\skills\solidworks-cad\
├─ docs\
├─ mcp-server-solidworks-main\
│  ├─ adapters\dsh\
│  ├─ compiler\solidworks\
│  └─ execution\solidworks\
└─ SolidWorksAiAssistant用户对话工作流.md
```

`dsh-integration` 下的 Skill 是安装事实源，`.dsh` 下为工作区同步镜像，两份内容必须保持一致。

## 安装与启动

要求：Windows、SolidWorks 2026、.NET Framework 4.8、MSBuild、Python 3，以及 `mcp>=2.0.0,<3`。

```powershell
Set-Location .\dsh-solidworks
& .\dsh-integration\scripts\Install-SolidWorksDsh.ps1 -DshRoot C:\path\to\deepseek-harness
& .\dsh-integration\scripts\Start-SolidWorksDsh.ps1 -DshRoot C:\path\to\deepseek-harness
```

诊断：

```powershell
& .\dsh-integration\scripts\Doctor-SolidWorksDsh.ps1 -DshRoot C:\path\to\deepseek-harness
```

恢复安装前配置：

```powershell
& .\dsh-integration\scripts\Restore-SolidWorksDsh.ps1 `
  -BackupRoot .\config-backups\<timestamp>
```

## Build

Create the adapter environment from `mcp-server-solidworks-main\requirements.txt`:

```powershell
py -3 -m venv .\mcp-server-solidworks-main\adapters\claude\.venv
& .\mcp-server-solidworks-main\adapters\claude\.venv\Scripts\python.exe -m pip install -r .\mcp-server-solidworks-main\requirements.txt
```

Build the .NET Framework execution service with MSBuild. Set
`SolidWorksInstallDir` when SolidWorks is installed outside the default path:

```powershell
msbuild .\mcp-server-solidworks-main\execution\solidworks\SolidworksExecution\SolidworksExecution.csproj /p:Configuration=Debug /p:SolidWorksInstallDir="C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS"
```

## 维护边界

- 高层工具注册以 `adapters/dsh/server.py` 为准。
- 低层 SolidPilot 工具只供 Python 编排器调用，不直接暴露给模型。
- C# 执行服务是唯一访问 SolidWorks COM 的层。
- Feature Graph 能力以 `cad-planner/contracts/feature-graph.schema.json` 为准。
- 低层 wire 协议以 `execution/solidworks/contracts` 为准。
- 工具、schema 或策略改变后，应同步 Skill、工具清单、插件白名单和定向测试。

## License

本仓库作为 SolidPilot 的衍生发行版，整体按 [GNU Affero General Public License v3.0](LICENSE) 发布。上游版权、修改说明和商标声明见 [NOTICE.md](NOTICE.md)。
