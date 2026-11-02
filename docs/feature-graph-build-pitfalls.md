# Feature Graph 构建失败的对策(实测)

> 开发笔记(2026-11-02 阀体实测整理,11-02 复核修正)。**不是**建模 agent 的参考资料,
> 不要链接进 SKILL.md;相切凸台和 hole 定位两条已写进服务端/编译器的报错提示。

这些不是语法错误(本地 `validate` 能拦的),而是**语义错误**——
语法合法、编译通过、到 SolidWorks 里才 null 或者对不上。每一条都给出机制和改法。

## hole 定位: `ref.at` 必须落在面平面上

`hole` 的 `ref.at` 第三坐标必须精确落在被选面的平面上(容差 1e-5 m),否则
`PLACEMENT_OFF_FACE`。哪个坐标是“第三坐标”取决于面的法线:

| 面 | 法线 | at 里必须对齐的坐标 |
| --- | --- | --- |
| `selector:"top"` | +Y | Y = 面高度 |
| front 方向平面 | +Z | Z = 面位置 |
| right 方向平面 | +X | X = 面位置 |

不要凭“差不多”写坐标。先 `inspect_state(scope="faces")` 读该面的 `point`/`normal`,
把坐标抄上去。

**多孔的另一种写法**:同一平面上多个**真正贯通、且孔轴线后方没有其他实体**的孔,
可以用一个 sketch 画多个 `circle` + 一次 `extrude`(`operation:"cut"`)。
`through_all` 会把孔轴线后方的所有材料一起切穿,所以只在后方确实没有实体时用;
否则用盲孔深度(如板厚)或 `up_to`。螺纹孔、沉孔、盲孔仍用 `hole`。

## 相切凸台会 EXTRUSION_FAILED

草图平面与被加材料的圆柱面**相切**时,没有材料重叠,SolidWorks 直接返回 null:
`Feature creation returned null for feature_type 'boss'`。

侧向凸台从圆柱侧面长出时,把草图放在过圆柱轴线的平面上(或任何能让凸台轮廓
完全落在已有实体内部的位置),再拉伸到凸台端面,深度按这个起始面重新计算。
不要用固定的“往里推多少 mm”:小圆柱会推穿,凸台轮廓比该处弦宽时还会在圆柱外
多出材料。同理,任何 boss 的草图面都应保证与已有实体有**面积**重叠,
而不只是相切接触。

根因在 `execution/solidworks/SolidworksExecution/bin/Debug/execution.log` 里能直接看到:
失败前最后一次 `create_sketch` / `extrude_feature` 的参数与状态。

## 弧链: 弧心不能在端点

`arc` 的 `cx,cy` 必须在**两端点各自半径处**;半圆弧的圆心是**弦中点**,不是端点。
把圆心写在端点上会报 `has an endpoint on its centre`。

跑道形/腰形轮廓手写时用 4 段:两条 `line` + 两条 `arc`,arc 圆心在弦中点。
`dir` 决定半圆往哪一侧凸(1 = 从起点到终点逆时针,-1 = 顺时针),两个值画出的是
方向相反的两个半圆;两端都必须向槽外凸,要按轮廓走向逐段确定,不能随便取。
能用 `slot` 原语(带 `angle`)就直接用,手写弧链只在
slot 表达不了时才用。

## 材质名不在库里只是 warning

`graph.material` 写库里没有的名字(如 HT200)→ `MATERIAL_NOT_FOUND` warning,
几何完整保留、不阻断、不影响 trust。不要为了零警告删掉图上写明的材料:保留材料名,
在最终回复里如实说明未应用;库里有对应牌号时,之后用 `apply_document_edits` 补上
(换成别的牌号前先问用户)。

## 体积/包络自查

建完用 `inspect_state(scope="mass_properties")` 读 `bbox_size` 与 `volume`,
对照计划里按图算出的预期值。偏差大先查“凸台方向反了”和“切除深度方向反了”
——`reversed` 的标志位是最常见的错位来源。
