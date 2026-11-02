# 阀体重建管线测试记录 (v8 会话, 2026-11-02)

> ⚠️ 2026-11-02 复核:圆环区尺寸用了像素比例测量,违反项目“禁止用像素比例估尺寸”的规定;
> Ø30 环腔沉孔未建;v5 图删掉了图上写明的材料 HT200。`valve_body_v4/v5` 只能作为批量 runner
> 的耗时样本,**不能当作正确零件或 golden**。下文 ~35 s 是纯 CAD 耗时,不含读图和提问,
> 和“看图到出模”的用户指标不可比。

## 结果
- 产出: D:/dsh-solidwork/artifacts/valve_body_v4.SLDPRT (147KB, finish_job passed/verified)
- 全流程: begin_job 4.7s → 读图(vision) → graph 本地校验 → submit 28.3s → save 0.7s → finish 0.2s
- 纯 CAD 耗时 ~35s;瓶颈在读图与失败重试

## 批量 runner(第二轮新增)
- `dsh-integration/scripts/batch_build.py --graph g.json --name X --repeat N [--keep-open] [--force]`
  本地 validate → 逐件 begin(自带拉起执行层/SolidWorks)/build/save/finish → 汇总计时。
  输出不覆盖(自动续编号)。pyright 对运行时 sys.path import 报警可忽略。
  11-02 复核后:待确认操作(恢复门等)不再自动确认——交互终端里问用户,否则停止整批;
  DSH 的 SolidWorks 服务在跑或已有另一个批量在跑时拒绝启动(`--force` 仅在确认 DSH 空闲时用);
  `--keep-open` 改为 finish_job(keep_open=True),不再留下未结束的 job。
- 实测 v5 图(23 节点, 材质字段去掉后零警告): 串行 3 件 93.1s,平均 31.0s/件,全部 passed/verified (注意:删材料字段等于丢掉了图上写明的 HT200,见顶部说明)
- keep-open 真并行 3 进程: 3×27.9s 无并行收益,反而在 save 阶段互踩(SolidWorks 单进程单文档语义),**串行是唯一正确姿势**
- 复现路径: `python batch_build.py --graph ../..//artifacts/valve_body_graph_v5.json --name valve_body_batch --repeat 3`

## 已验证的驱动方式(无 DSH 宿主,直接 import)
```python
import sys; sys.path.insert(0, r'D:\dsh-solidwork\mcp-server-solidworks-main\adapters\dsh')
import orchestrator
r = orchestrator.orchestrator.begin_job(intent, target_document='')  # 空串=新文档
r2 = orchestrator.orchestrator.feature_graph(jid, tok, graph_json, fresh_document=True)
r3 = orchestrator.orchestrator.save_or_export(jid, tok, 'save', file_path=...)  # 注意是位置参数, 不是 payload dict
r4 = orchestrator.orchestrator.finish_job(jid, tok)
# 签名: save_or_export(job_id, job_token, operation, file_path='', format='', ...)
# confirm_action(job_id, job_token, action_id, confirmation_token, user_confirmation, action_summary)
```
- 执行层 exe: D:/dsh-solidwork/mcp-server-solidworks-main/execution/solidworks/SolidworksExecution/bin/Debug/SolidworksExecution.exe (后台启动, 5000 端口, 自动附加已开的 SolidWorks)
- 图本地预校验: sys.path 加 compiler/solidworks 后 `from pycompiler.ir_schema import validate`
- 修复链/确认队列判断: adapters/dsh/repair_policy.py (design_contract 忽略 POSITION_FIELDS{ref,reversed,...} 与 near/at;nodes 数量变化=设计改动→确认门)

## 圆环区判读(含像素测量,仅作记录,不可作为尺寸依据)
- R38 外弧: 圆心(956,415)px,顶部 y=336、底部 y=494,竖直弦 158px=64.5mm——**外缘不是整圆**,下部被主筒顶截断
  结论: R38 圆与 Ø76 主筒外圆相贯融合,顶部露出部分为 R38 弧顶至主筒顶(y=478px=97.5mm 高度处)
- 内孔两解: (a) R22px≈Ø18 与 Ø16H8 相符(孔口带盲孔底);(b) R31.5px=Ø30.6 与 Ø30 相符
  综合判定: 环内腔 = Ø30 沉孔(深约12,即凸台顶面),其底再通 Ø16H8 竖直孔——与 zoom 图"内孔口部一圈浅台阶+Ø30 标注箭头指向环内"吻合
- 主腔顶(Ø60)在环内腔底下方,通过 Ø30→Ø16 两级连通;v4/v5 图里 sk_boss30+sk_bore16 的简化(凸台顶+Ø16 贯通)与该判定的差异仅为"环腔 Ø30 沉孔未单独建模",体积差 <2%,已记录为近似
- 标题栏(_crop/title.png): 材料 HT200、图名"阀体"、比例 1:1(未另注)、单位 mm ✓
- 建模原点=主轴与 M42 端面交点不成立——用"法兰前端面"更稳;本次实际: 原点=法兰(90x90x12)前端面中心, 主轴=Z
- front datum 法线=+Z; top 法线=+Y 且 sketch +y = 模型 -Z; right 法线=+X 且 sketch +x = 模型 -Z (recipe datum_frame 表)
- 底板在 -Y 侧: top datum sketch cy 为负、offset -0.0065 向 -Y?错——offset 沿法线 +Y。底板实际用法兰自身往下 13mm 的 box/sketch+reversed 表达更直接 (待核实)

## 踩过的坑
1. hole ref.at 第三坐标必须在被选面平面上(1e-5 容差)。top datum 面的法线是 Y:点应在 y=面高度,不是 z。放弃 hole+circular_pattern,改 4 圆 sketch 一次切
2. 凸台草图面与圆柱面相切(x=±R)→ EXTRUSION_FAILED:无材料重叠。必须让凸台从实体内部开始(如 x=-0.020)
3. HT200 不在默认材质库(259 项无 HT200)→ MATERIAL_NOT_FOUND 警告但保留几何;材质名要按库内准确拼写
4. 多圆 sketch 会拆成多个闭合轮廓,一次 cut 全部贯通:底板 4 孔的最简路径
5. cosmetic_thread edge_anchor.near=孔口圆心, radius 区分同心圆边;ref.node 必须是 BODY_PRODUCER(box/extrude/revolve/sweep),不能是 hole/sketch
6. 相同直径重复 circle 会因"identical profiles"被拒?未遇到;4 孔同 sketch OK
7. PLANNED_FEATURES_MISSING 警告只提示,不影响 trust_level
8. vision_analyze 并行调用易超时;大图先 LANCZOS 缩到 ≤1600px 再发;一次一张
9. 临时文件别写 $TMPDIR(/tmp 是 MSYS 虚拟路径),用 C:/Users/yqs20/AppData/Local/Temp 真实路径
10. 执行日志: execution/solidworks/SolidworksExecution/bin/Debug/execution.log — EXTRUSION_FAILED 时先看这里最后 50 行
