# SolidWorks MCP 工具清单

本文说明 `dsh-solidwork` 的两层工具边界：

1. 模型可见的 12 个高层工具。
2. 仅由编排器使用的低层 SolidPilot 与 C# 执行命令。

模型能力的事实源是：
`mcp-server-solidworks-main/adapters/dsh/server.py`。

## 1. 模型可见的 12 个工具

调用前缀为 `mcp__solidworks__`。

| 工具 | 风险 | 用途 |
| --- | --- | --- |
| `start_job` | 读/打开 | 连接 SolidWorks，按需打开现有文件，锁定活动文档身份并返回任务凭据。 |
| `inspect_state` | 只读 | 读取就绪状态、文档分析、选择集或受控 recipe/schema。 |
| `submit_feature_graph` | 写/批量 | 用现有确定性编译器执行完整 Feature Graph。 |
| `apply_document_edits` | 写/破坏性/批量 | 对目标文档执行白名单参数化编辑。 |
| `drawing_workflow` | 写/批量 | 新建工程图并编排视图、标注、表格和标准检查。 |
| `save_or_export` | 写/覆盖/另存为/关闭 | 保存、导出、批量导出或关闭。 |
| `request_confirmation` | 只读确认 | 验证待办动作并轮换一次性确认 token。 |
| `confirm_action` | 对应原动作 | 用户明确批准精确摘要后执行一个待办动作。 |
| `cancel_action` | 取消待办 | 取消待确认动作，不执行其 payload。 |
| `finish_job` | 验证 | 运行最终客观验证并结束任务。 |
| `read_drawing_text` | 只读 | 用 Windows 内置 OCR 一次读出图上所有文字及其位置和网格格；可选辅助，默认先直接看原图，不需要任务，不接触 CAD。 |
| `read_drawing_region` | 只读 | 放大工程图局部，读取小尺寸文字；不需要任务，不接触 CAD。 |

专用 profile 的策略插件只允许这 12 个 SolidWorks 工具，外加下方 6 个支撑工具。
任何其他 `mcp__solidworks__*` 工具都不属于模型可调用接口。

`read_drawing_region(image_path, region="", zoom=2)`：`image_path` 接受本地图片文件路径或当前 DSH 实例的 `sha256:<64位十六进制>` 附件 ID
（`http://`、`https://`、`ftp://`、`file://` 一律拒绝）；`region` 为空返回带字母数字网格的
全图（列 A–H 从左到右，行 1–6 从上到下），也可传网格标签如 `B2`，或源图像素框 `x0,y0,x1,y1`；
`zoom` 为 1–4 的整数。输出 PNG 写入 `artifacts\intake\<图片哈希>\`，并同时返回图片与一行 JSON
（输出路径、像素、字节、源像素框、网格定义）。超出图片范围的框会被拒绝而不是裁剪，因为被裁掉的
部分正是模型看不到的证据。
可选 `regions=[...]` 一次读取 1–12 个区域，单区与批量均可；保留资源限制，移除每图次数、两轮和重复区域闸门及模型 reset。策略按宿主 Agent/turn 检测连续 3 次无新证据/失败的读图，拦截重复尝试；新证据可恢复。读图结论、紧凑计划批准与全部真正缺尺寸合并一轮问答，答后直接建，工具故障不当作缺尺寸。

支撑工具没有 `mcp__solidworks__` 前缀，由同一个策略插件放行：

| 工具 | 用途 |
| --- | --- |
| `ask_user_question` | 向用户提出选择题式提问。 |
| `todo_write` | 维护任务待办清单。 |
| `skill` | 加载并读取技能。 |
| `modlens_read_image` | 读取图片证据；仅接受本地图片文件路径，`http://`、`https://`、`ftp://`、`file://` 等 URL 形式一律拒绝。 |
| `read_mcp_resource` | 按 URI 读取一份技能参考文档，形如 `solidworks://skill/references/<file>.md`；带路径或 `..` 的 URI 以 `SKILL_RESOURCE_TRAVERSAL_REFUSED` 拒绝，未知名字以 `SKILL_RESOURCE_NOT_FOUND` 拒绝；只读 SKILL.md 链接的 URI。本预设没有本地文件工具，这是读取参考文档的唯一入口。 |

SolidWorks 预设只向模型暴露上表的支持工具和 12 个 SolidWorks 工具（桌面版没有 modlens，共 16 个）；其余全局 MCP 工具由 solidworks-policy 插件在预设作用域内屏蔽，既不发送定义，也不允许调用。`list_mcp_resources` 自 2026-10-31 起不再开放。

## 2. `start_job`

输入：

- `intent`
- `target_document`
- `as_assembly`

输出包含 `job_id` 和私密 `job_token`。后续深层检查和所有写操作必须携带二者。

任务会记录目标文档标题、路径和类型。用户在 SolidWorks 中切换活动文档后，下一次任务调用会因身份不一致而停止。

`target_document` 留空且当前没有活动文档时，任务处于未绑定状态：它只能通过任务自身的建文档调用绑定，即 `submit_feature_graph` 且 `fresh_document=true`。若留空时已有活动文档，任务将该文档绑定为只读。此任务不得保存、另存为、导出、关闭或编辑该文档，也不得以 `fresh_document=false` 建模；这些写操作在任何执行层调用前以 `JOB_DOCUMENT_READ_ONLY` 拒绝。两种情况下，待确认 Feature Graph 均会阻止其他文档写操作，以 `GRAPH_PENDING_CONFIRMATION` 提示先确认或用 `cancel_action` 取消。`close_all=true` 也不得绕过这些保护。无活动文档的未绑定任务仍可通过已确认的 `close_all` 执行显式全局关闭，但不会建立文档绑定。

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
- `feature_graph_quickref`（由 schema 自动生成的速查卡：每个节点类型的含义、必填/可选字段与单位，1-2 页）
- `feature_graph_changelog`（schema 的修订历史）

参考内容按页返回：每次调用在 `verification` 内给出 `topic`、`content` 与 `page`（`offset`、`limit`、`returned_lines`、`total_lines`、`total_chars`、`has_more`、`next_offset`、`line_truncated`）。`reference_offset`/`reference_limit` 默认 `0`（使用默认值），可请求更小的页，更大的值被钳制；每页内容上限约 6820 字符，序列化后的整页（含封套）不超过 7000 字符，至多 150 行。`page.has_more` 为 true 时用 `page.next_offset` 继续读取，或用 `reference_offset` 跳到 `page.index.sections[].line`。`feature_graph_schema` 的 `page.index` 还包含 `sections`（`section`、`line`、`entries`）与 `feature_types`（节点类型列表）。构建前的默认预读是 `feature_graph_quickref` + 一个 mapping 节 + `verification`，约 5 次调用。

## 4. `submit_feature_graph`

用于新建零件和装配体。

- `graph_json` 必须是完整 Feature Graph。
- 节点按特征树依赖顺序排列。
- Feature Graph 长度使用米。
- Feature Graph 角度使用弧度。
- 常规新建任务使用 `fresh_document=true`。
- 大图会返回 `pending_confirmation`。

自上一发布版本以来新增的节点与参数（以 `feature-graph.schema.json` 0.8.0-draft 为准）：

- `hole.depth` 接受以米为单位的正数（盲孔），或 `through_all`（`through` 为别名）。
- `revolve.operation` 支持 `boss`（默认）与 `cut`；轴取草图中心线的精确 2D 端点，由编译器确定性解析。
- `sweep.sketch` 明确引用轮廓草图节点（须为紧邻的前一节点）。
- 新节点类型 `shell`（抽壳）：`body` 引用更早的成体节点，`thickness` 为壁厚（米），可选 `faces_to_remove` 面锚点（开放面）与 `outward`。`InsertFeatureShell` 无返回值，成功与否按特征树新增加未舍入的体积差共同确认（体积不变的报错特征会被判为 `SHELL_FAILED`）。
- 轮廓图元 `slot` 增加 `angle`（弧度，逆时针，绕 `(cx, cy)`），`rectangle`、`slot`、`rounded_rect`、`circle` 均接受可选 `cx`/`cy` 中心。
- 草图为闭合轮廓时，同一草图内互相重叠的闭合轮廓在计划阶段即被拒绝（`VALIDATION_FAILED`），不再等到 SolidWorks 构建中报 `EXTRUSION_FAILED`。

返回结果包含编译节点日志、CAD 状态和写后验证。节点执行成功但客观验证失败时，事务会尝试回滚。`fresh_document=true` 构建失败且恢复丢弃了新建文档时，失败结果以该文档为写入目标，并带 `created_document_discarded` 与 `restored_active_document` 字段。

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

保存时指定路径同样需要确认（`risk = save_as`）：给未命名文档的首次保存——包括任务刚创建的文档——以及把已保存文档保存到不同路径，都是 SolidWorks 的另存为，会在内存中重命名打开的文档；`action_summary` 会同时点名文档和目标路径。不带 `file_path` 的原地保存仍按覆盖现有文件的规则确认。

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

这些名字不是 DSH 模型接口。模型需要的能力必须通过 12 个高层工具表达。

## 12. 事实来源

- 高层工具注册：`mcp-server-solidworks-main/adapters/dsh/server.py`
- 编排和事务：`mcp-server-solidworks-main/adapters/dsh/orchestrator.py`
- 安全策略：`mcp-server-solidworks-main/adapters/dsh/policy.py`
- DSH 白名单：`dsh-integration/plugins/solidworks-policy/index.js`
- Feature Graph schema：`mcp-server-solidworks-main/cad-planner/contracts/feature-graph.schema.json`
- C# wire schema：`mcp-server-solidworks-main/execution/solidworks/contracts/tool-schemas.json`
- DSH Skill：`dsh-integration/presets/solidworks-cad/skills/solidworks-cad/SKILL.md`

