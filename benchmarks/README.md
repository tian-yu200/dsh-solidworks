# dsh-solidworks 2D-to-3D 可复跑基准评测集（Benchmark Suite）

本目录提供一套**不强依赖启动真实 SolidWorks 即可离线校验与汇总评分、同时原生兼容 `batch_build.py` 实机重放输出**的标准化 2D-to-3D 基准评测脚手架，用于解决项目早期“只有单次零散实测、缺少可复跑评测基线”的工程缺口。

---

## 一、核心评测哲学：摒弃“炫技率”，锚定“熟手人效比”与“可编辑性”

在一线中小非标制造私企与 CAD 代画熟手的真实工作流中，单纯追求“AI 零人工介入一次生成 100% 完美零件”既不现实也非痛点所在；反之，**如果 AI 折腾 15 分钟建出一个外形勉强相似、但双击改一个孔距就全树爆红报错的“死模型”，熟手还不如自己花 12 分钟从头手画（即“AI 试错税倒挂”）**。

因此，本基准集确立两大硬核评判准则：

1. **北极星指标——熟手人效耗时比 $R$**：
   $$R = \frac{T_{\text{ai}} + T_{\text{edit}}}{T_{\text{human}}}$$
   - $T_{\text{ai}}$：AI 读图规划 + 一轮提问确认 + 确定性编译建模耗时（分钟）；
   - $T_{\text{edit}}$：SolidWorks 熟手终审并补齐局部细节（如未注铸造圆角、异形孔向导标准件属性）的微调耗时（分钟）；
   - $T_{\text{human}}$：同等熟练度工程师从零看图纯手工建模基准耗时 `human_baseline_minutes`（分钟）。
   - **商业正收益准入线**：$R \le 0.50$（即综合省时 $\ge 50\%$，单台正版工作站有效翻模产能放大 $\ge 2.0\text{x} \sim 3.0\text{x}$）；若 $R > 0.80$，触发 `NEGATIVE_ROI` 试错税倒挂告警。
2. **一票否决门禁——特征树可编辑性（Feature-Tree Editability）**：
   - 每个基准条目在 `manifest.json` 中预定义 **1~2 项主尺寸修改检查（`editability_checks`）**（如轴段加长、底板加厚、孔径/孔距变更）。
   - 在 SolidWorks 中修改对应草图/特征尺寸并按 `Ctrl+B`（或强力重建 `Ctrl+Q`）后，**必须零特征报错（红叉/黄叹号）、零悬空边面引用、零非预期多实体分裂**，否则一律判为 `FRAGILE_TREE` 不予通过。

---

## 二、目录结构

```text
benchmarks/
├─ manifest.json                         # 基准评测集 Schema、五大高频件分类与种子用例清单
├─ evaluate_benchmark.py                 # 离线契约校验、batch_build 对接与 Markdown 评分报表生成器
├─ README.md                             # 基准集维护与评测运行指南（本文档）
├─ fixtures/
│  ├─ sample_stepped_shaft_graph.json    # M5双端盲孔阶梯轴标准 Feature Graph（可直接供 batch_build.py 重放）
│  ├─ sample_eval_runs.json              # 内置端到端评分自检夹具（含有效视觉样本与无视觉污染对照反例）
│  └─ sample_batch_build.log             # batch_build.py 控制台输出解析测试夹具
└─ tests/
   └─ test_evaluate_benchmark.py         # 评测脚手架单元测试套件
```

> **说明**：原始客户图纸图片或大型本地 `.SLDPRT` 文件按仓库 `.gitignore` 约定可存放在本地 `benchmarks/drawings/local/`（不随公开 AGPL 仓库分发未授权客户原图），公开仓库仅保留脱敏元数据、理论几何账本与确定性示例图。

---

## 三、收录范围：五大中小私企高频件 vs 主动排除边界

### 1. 五大目标分类（`category_taxonomy`）
| 分类代码 | 分类名称 | 典型工业零件 | 核心考察能力 |
| --- | --- | --- | --- |
| `bracket` | 支架类 | 2-25 圆头长槽支架、叉耳支座、带筋角码 | 多视图投影对齐、圆弧相切轮廓、`slot` 腰形槽、`rib` 筋板 |
| `shaft_flange` | 轴套法兰类 | M5双端盲孔扁位阶梯轴、法兰端盖、衬套 | `revolve` 旋转阶梯体、轴向尺寸链推导、对称切扁位、同轴盲孔/装饰螺纹 |
| `housing_valve` | 座体箱体类 | HT200 阀体、脚踏座 LJT05.12、轴承座 | 多向管腔正交相贯、避免相切零厚凸台、`shell` 抽壳、内切大圆弧推导 |
| `plate_block` | 板块连接件 | 五边异形底板高台支座、斜面缺口治具块、钥匙轮廓件 | 多边形切角草图、多孔一次切除/线性阵列、阶梯斜面体 |
| `sheet_metal` | 钣金件 | U 形折弯电控护罩、激光下料折弯托架 | `sheet_metal` 基体法兰、`edge_flange` 边线法兰、板厚变更与展开无干涉 |

### 2. 主动排除范围（`excluded_categories`）
- **`freeform_surface`（高阶自由曲面）**：汽车 A 面、汽轮机扭曲叶片流道、复杂人体工学曲面造型——不纳入评测，产品层面公开声明不做。
- **`drawingless_concept`（无图概念生成）**：无工程图尺寸约束的口头创意生成——严禁入库。

---

## 四、快速运行指南

推荐使用仓库已配置的适配器虚拟环境 Python（无需启动 SolidWorks 即可跑完离线自检与报表汇总）：

```powershell
$PY = "D:\dsh-solidwork\mcp-server-solidworks-main\adapters\claude\.venv\Scripts\python.exe"

# 1. 一键运行全链路离线自检（校验 manifest + 编译器校验示例图 + 解析 batch_build 日志 + 输出样例 Markdown 报告）
& $PY .\benchmarks\evaluate_benchmark.py --self-check

# 2. 仅校验 manifest.json 与关联 Feature Graph 的契约合法性（适合放入 CI）
& $PY .\benchmarks\evaluate_benchmark.py --validate-only

# 3. 传入完整实测运行记录 JSON，输出正式 Markdown 评测报告与机器可读汇总
& $PY .\benchmarks\evaluate_benchmark.py `
  --results .\benchmarks\fixtures\sample_eval_runs.json `
  --output .\artifacts\bench_report.md `
  --json-summary .\artifacts\bench_summary.json

# 4. 在装有 SolidWorks 的本机上用 batch_build.py 实机重放示例阶梯轴，并将日志直接喂给评测器
& $PY .\dsh-integration\scripts\batch_build.py `
  --graph .\benchmarks\fixtures\sample_stepped_shaft_graph.json `
  --name shaft_stepped_m5 | Tee-Object -FilePath .\artifacts\shaft_batch.log

& $PY .\benchmarks\evaluate_benchmark.py --batch-log .\artifacts\shaft_batch.log

# 5. 运行评测脚手架单元测试
& $PY -m pytest -q .\benchmarks\tests\test_evaluate_benchmark.py
```

---

## 五、如何添加新基准图纸（学长复测件 & 代画真实工业“脏图纸”入库 SOP）

为了防止基准集沦为“干净教科书温室”，新增用例应优先采集**一线真实工业“脏图纸”**（如手机斜拍纸质蓝图、低分辨率扫描 PDF、带手写公差修改批注、存在未注圆角/倒角缺省约定的非标机加工与钣金图纸，**严禁收录大学生制图作业**）。

### 标准入库五步法

1. **视觉环境前置核验（复测前必做）**
   在采集实测数据（尤其是复测 `retest_senior_key_part` 学长钥匙件与 `retest_senior_bracket_part` 学长支架件）前，必须先运行视觉能力守卫，杜绝落入 `DeepSeek-V4.1-Flash` 等纯文本无视觉模型的历史污染：
   ```powershell
   node .\dsh-integration\scripts\Check-ModelImageCapability.mjs
   & .\dsh-integration\scripts\Doctor-SolidWorksDsh.ps1
   ```
   确认 `Model image capability` 为 `True` 后，方可开始计时实测。

2. **商业脱敏与文件归档**
   - 裁切或涂抹图纸标题栏中的客户企业全称、手机号、保密产品代号；
   - 将脱敏后的图片放入本地 `benchmarks/drawings/local/<case_id>.png`（多视图拆图可存为数组）。

3. **人工精算锁定 Golden 真值（严禁盲信 AI 自算值）**
   - 由 SolidWorks 熟手对照原图核算出精确的三轴外包络 `expect_mm: [X, Y, Z]`；
   - 对于尺寸完全封闭的机加工件，填入解析体积或人工核验标准模型的体积 `theoretical_volume_mm3`（容差建议 `0.01`）；对于含未注铸造圆角的壳座件，可将 `theoretical_volume_mm3` 设为 `null`，重点依靠 `dimension_ledger` 关键孔径/中心距核验；
   - 在 `dimension_ledger` 中逐条列出 `explicit`（明示尺寸）与 `derived`（尺寸链推导尺寸，如 `37-8-5.5-22=1.5`）。

4. **标定 `human_baseline_minutes` 与 `editability_checks`**
   - 按中等偏上熟练度的机械技术员从零看图建模（含读图、草图约束、特征构建、质量属性自查）的实际分钟数填写 `human_baseline_minutes`；
   - 设计 **1~2 项最具代表性的改尺寸动作**写入 `editability_checks`（例如“底板孔距从 70 改为 76”、“钣金板厚从 2.0 改为 2.5”），明确 `from_mm`、`to_mm` 及 `must_not_trigger` 禁止出现的错误类型。

5. **运行静态门禁确认零错误**
   编辑保存 `benchmarks/manifest.json` 后，立即运行：
   ```powershell
   & $PY .\benchmarks\evaluate_benchmark.py --validate-only
   ```
   返回 `OK` 即完成新用例入库。
