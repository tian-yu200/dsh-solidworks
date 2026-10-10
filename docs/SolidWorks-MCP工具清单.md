# SolidWorks MCP 工具清单

本文说明 `dsh-solidwork` 的两层工具边界：

1. 模型可见的 9 个高层工具。
2. 仅由编排器使用的低层 SolidPilot 与 C# 执行命令。

模型能力的事实源是：
`mcp-server-solidworks-main/adapters/dsh/server.py`。

## 1. 模型可见的 9 个工具

调用前缀为 `mcp__solidworks__`。

| 工具 | 风险 | 用途 |
| --- | --- | --- |
| `start_job` | 读/打开 | 连接 SolidWorks，按需打开现有文件，锁定活动文档身份并返回任务凭据。 |
| `inspect_state` | 只读 | 读取就绪状态、文档分析、选择集或受控 recipe/schema。 |
| `submit_feature_graph` | 写/批量 | 用现有确定性编译器执行完整 Feature Graph。 |
| `apply_document_edits` | 写/破坏性/批量 | 对目标文档执行白名单参数化编辑。 |
| `drawing_workflow` | 写/批量 | 新建工程图并编排视图、标注、表格和标准检查。 |
| `save_or_export` | 写/覆盖/关闭 | 保存、导出、批量导出或关闭。 |
| `request_confirmation` | 只读确认 | 验证待办动作并轮换一次性确认 token。 |
| `confirm_action` | 对应原动作 | 用户明确批准精确摘要后执行一个待办动作。 |
| `finish_job` | 验证 | 运行最终客观验证并结束任务。 |

专用 profile 的策略插件只允许这 9 个 SolidWorks 工具，外加下方 4 个支撑工具。
任何其他 `mcp__solidworks__*` 工具都不属于模型可调用接口。

支撑工具没有 `mcp__solidworks__` 前缀，由同一个策略插件放行：

| 工具 | 用途 |
| --- | --- |
| `ask_user_question` | 向用户提出选择题式提问。 |
| `todo_write` | 维护任务待办清单。 |
| `skill` | 加载并读取技能。 |
| `modlens_read_image` | 读取图片证据；仅接受本地图片文件路径，`http://`、`https://`、`ftp://`、`file://` 等 URL 形式一律拒绝。 |

## 2. `start_job`

输入：

- `intent`
- `target_document`
- `as_assembly`

输出包含 `job_id` 和私密 `job_token`。后续深层检查和所有写操作必须携带二者。

任务会记录目标文档标题、路径和类型。用户在 SolidWorks 中切换活动文档后，下一次任务调用会因身份不一致而停止。

`target_document` 留空且当前没有活动文档时，任务处于未绑定状态：它只能通过任务自身的建文档调用绑定，即 `submit_feature_graph` 且 `fresh_document=true`。其余所有依赖文档的调用——`apply_document_edits`、`submit_feature_graph` 且 `fresh_document=false`、`drawing_workflow`、save/export/batch/close，以及执行非建文档待办动作的 `confirm_action`——都会在发起任何执行层调用前失败并返回 `JOB_DOCUMENT_UNBOUND`。此时需要用 `target_document` 重新 `start_job`，或先打开文档再启动新任务。`save_or_export` 的 `close` 且 `close_all=true` 在未绑定任务上允许执行，但不会建立绑定。

## 3. `inspect_state`

无需任务凭据即可使用：

- `scope="state"`
- `scope="reference"`

文档深层读取必须提供 `job_id` 和 `job_token`。

| 文档类型 | scope |
| --- | --- |
| 零件 | `features`、`geometry`、`mass_properties`、`bodies`、`faces`、`edges`、`selection` |
| 装配体 | `components`、`components_flat`、`mates`、`faces`、`edges`、`selection` |
| 工程图 | `drawing`、`selection` |

装配体 `faces` 和 `edges` 还必须指定 `component`。

可读 reference：

- `contract`
- `canonicalization`
- `forward`
- `mapping`
- `mapping_part`
- `mapping_sheet_metal`
- `mapping_assembly`
- `verification`
- `reverse`
- `coverage`
- `feature_graph_schema`

参考内容按页返回：每次调用在 `verification` 内给出 `topic`、`content` 与 `page`（`offset`、`limit`、`returned_lines`、`total_lines`、`total_chars`、`has_more`、`next_offset`、`line_truncated`）。`reference_offset`/`reference_limit` 默认 `0`（使用默认值），可请求更小的页，更大的值被钳制；每页至多 40 行、约 4000 字符。`page.has_more` 为 true 时用 `page.next_offset` 继续读取，或用 `reference_offset` 跳到 `page.index.sections[].line`。`feature_graph_schema` 的 `page.index` 还包含 `sections`（`section`、`line`、`entries`）与 `feature_types`（节点类型列表）。

## 4. `submit_feature_graph`

用于新建零件和装配体。

- `graph_json` 必须是完整 Feature Graph。
- 节点按特征树依赖顺序排列。
- Feature Graph 长度使用米。
- Feature Graph 角度使用弧度。
- 常规新建任务使用 `fresh_document=true`。
- 大图会返回 `pending_confirmation`。

返回结果包含编译节点日志、CAD 状态和写后验证。节点执行成功但客观验证失败时，事务会尝试回滚。

## 5. `apply_document_edits`

`operations_json` 是 `{tool, params}` 数组，只允许：

| 内部编辑动作 | 用途 |
| --- | --- |
| `modify_dimension` | 修改命名驱动尺寸 |
| `edit_feature` | 抑制、解除抑制、删除或重命名特征 |
| `set_part_material` | 设置零件材料 |
| `add_edge_feature` | 添加圆角或倒角 |
| `create_pattern` | 线性、圆周或镜像阵列 |
| `add_reference_geometry` | 创建参考面、轴或点 |
| `add_sketch_constraint` | 添加草图约束 |
| `insert_toolbox_component` | 从 Toolbox 映射插入标准件 |
| `create_exploded_view` | 创建装配体爆炸视图 |

删除和大批量编辑需要确认。结构编辑后应重新读取特征、面或边，不继续依赖旧索引。

## 6. `drawing_workflow`

`workflow_json` 可以编排：

- 标准模型视图和显示模式
- 钣金展开视图
- 剖视图
- 局部视图
- 模型标注导入
- 自动尺寸
- 自动中心标记
- BOM
- 自动气泡
- 孔表
- 工程图标准检查

长工作流返回 `pending_confirmation`。工程图完成后会检查文档类型、重建状态、引用解析和适用的工程图结果。

## 7. `save_or_export`

支持：

- `save`
- `export`
- `batch_export`
- `close`

导出格式：

- STEP
- IGES
- STL
- PDF
- DWG
- DXF

PDF、DWG 和 DXF 需要活动文档为工程图。覆盖现有文件、批量导出、关闭全部文档和丢弃未保存修改需要确认。

输出验证至少检查目标文件存在且非空。

## 8. 确认协议

任何写工具都可能返回：

```text
status = pending_confirmation
action_id
action_summary
```

`action_summary` 会点名目标文档：优先使用任务锁定的文档路径，其次使用文档标题，再回退到当前活动文档的路径或名称。

正确流程：

1. 使用 `request_confirmation(job_id, job_token, action_id)` 获取新的一次性 token、精确 `confirmation_text` 和精确 `action_summary`。
2. 原样展示 `action_summary`。
3. 等待用户在新消息中明确批准。
4. 使用 `confirm_action` 原样提交 `action_id`、一次性 token、`confirmation_text` 和 `action_summary`。

原始请求不视为危险动作批准。token 不能复用。风险或摘要变化后必须重新确认。

## 9. 响应与恢复

高层响应统一包含：

- `status`
- `risk`
- `job_id`
- `target_document`
- `backup_path`
- `completed_steps`
- `partial_state`
- `verification`
- `error`
- `execution_status`
- `verification_status`
- `recovery_status`
- `trust_level`

| status | 含义 |
| --- | --- |
| `completed` | 本次调用完成，仍需检查验证字段。 |
| `pending_confirmation` | 已排队，未执行。 |
| `failed` | 执行失败并成功回滚，或在写入前失败。 |
| `partial` | 回滚失败，文档可能保留部分修改。 |
| `verification_failed` | 写入已执行，但客观验证未通过。 |

| recovery_status | 含义 |
| --- | --- |
| `not_needed` | 未触发恢复。 |
| `rolled_back` | Undo 和回读验证成功。 |
| `rollback_failed` | 无法证明文档已恢复。 |
| `backup_only` | 只有文件备份，没有完成 Undo 恢复。 |

`error.code` 取值：执行层失败返回 `EXECUTION_LAYER_ERROR`；带 `UPPER_SNAKE` 前缀的 `RuntimeError` 直接返回该前缀（例如 `ACTIVE_DOCUMENT_CHANGED`、`BACKUP_SOURCE_MISSING`、`JOB_DOCUMENT_UNBOUND`）；其余未预期错误返回 `UNEXPECTED_ERROR`。

## 10. Trust Level

| trust_level | 含义 |
| --- | --- |
| `unverified` | 未验证，不能宣称完成。 |
| `built_unverified` | 已构建，但关键客观证据不可用。 |
| `drawing_consistent` | 工程图方向证据一致，但没有参考实体比较。 |
| `verified` | 最终客观验证通过。 |

## 11. 编排器内部低层命令

以下能力存在于 `adapters/claude/server.py`、Python backend 或 C# 执行服务中，但只供编排器实现高层工作流：

| 类别 | 代表命令 |
| --- | --- |
| 生命周期 | `ensure_ready`、`open_document`、`open_new_part`、`open_new_assembly`、`verify_state` |
| 分析 | `analyze_model`、`analyze_assembly`、`analyze_slddrw_test`、`get_selection` |
| 草图/特征 | `create_sketch`、`add_sketch_entities`、`extrude_feature`、`sheet_metal_feature` |
| 参数编辑 | `modify_dimension`、`edit_feature`、`create_pattern`、`add_edge_feature` |
| 装配体 | `insert_component`、`add_mate`、`insert_toolbox_component`、`create_exploded_view` |
| 工程图 | `create_drawing`、`add_drawing_view`、`add_section_view`、`create_detail_view`、`import_model_annotations`、`create_bom`、`add_balloons`、`create_hole_table`、`check_drawing_standards` |
| 输出 | `save_document`、`export_document`、`batch_export`、`close_document` |
| 事务 | `begin_undo_scope`、`commit_undo_scope`、`rollback_undo_scope` |

这些名字不是 DSH 模型接口。模型需要的能力必须通过 9 个高层工具表达。

## 12. 事实来源

- 高层工具注册：`mcp-server-solidworks-main/adapters/dsh/server.py`
- 编排和事务：`mcp-server-solidworks-main/adapters/dsh/orchestrator.py`
- 安全策略：`mcp-server-solidworks-main/adapters/dsh/policy.py`
- DSH 白名单：`dsh-integration/plugins/solidworks-policy/index.js`
- Feature Graph schema：`mcp-server-solidworks-main/cad-planner/contracts/feature-graph.schema.json`
- C# wire schema：`mcp-server-solidworks-main/execution/solidworks/contracts/tool-schemas.json`
- DSH Skill：`dsh-integration/presets/solidworks-cad/skills/solidworks-cad/SKILL.md`

