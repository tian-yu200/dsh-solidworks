# DSH 模型图片输入配置

文档目的：说明 DSH（DeepSeek Harness）如何判定一个模型能否接收图片附件，以及换模型、加模型时如何保持图片能力可用。CAD 参考图重建工作流依赖此能力。

## 判定机制

发送包含图片的消息前，DSH 的 LLM 适配层（`llm-pi-ai`）按以下顺序解析模型的输入能力，解析结果不含 `image` 时直接拒绝消息（报 `UNSUPPORTED_CONTENT`：`model "..." does not support image input`）：

```text
模型条目显式声明的 input
  -> 内置模型目录（仅当 provider 名是目录 provider）
  -> provider 级 defaultInput
  -> 硬编码默认 ["text"]
```

两类 provider 的行为不同：

| 类型 | 例子 | 能力来源 | 需要配置吗 |
| --- | --- | --- | --- |
| 目录 provider | `deepseek`、`openai`、`nvidia`、`anthropic` 等 36 个 | 内置目录逐模型精确声明 | 不需要，目录即事实源 |
| 自定义路由 | 指向自建/第三方代理的任意名字 | 目录查不到，落到 `defaultInput` | 需要，见下 |

注意：官方 DeepSeek API 的 `deepseek-*` 模型是纯文本模型（目录声明 `["text"]`），无论怎么配置都无法接收图片。图片任务必须路由到声明了 `image` 输入的模型。

## 本仓库约定

`~/.dsh/settings.yaml` 中每个自定义路由 provider 一律显式声明 `defaultInput: [ text, image ]`；个别确认无视觉能力的模型再在模型条目上用 `input: [ text ]` 显式覆盖（模型条目声明优先于 provider 默认值）。

```yaml
llm-pi-ai:
  providers:
    open:                          # 示例：自定义代理
      apiKeyEnv: OPEN_API_KEY
      api: openai-responses
      baseURL: https://example.invalid/
      defaultInput: [ text, image ]   # provider 级：之后新增的模型自动可收图
      models:
        - id: some-model
          name: some-model
        - id: text-only-model
          input: [ text ]          # 例外：显式降级为纯文本
```

这样换模型、加模型时不需要逐个补声明；只有刻意声明为 `[ text ]` 的模型会被拒绝图片。

## 图片大小预算

provider 级可调，默认值：

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `maxRequestImageBytes` | 20 MiB | 整个请求 base64 图片负载上限 |
| `requestImagePixelBudget` | 4 Mi 像素 | 降采样目标像素数 |
| `requestImageMaxBytes` | 1 MiB | 降采样后单张图片上限 |

工程图截图读尺寸时若小字被降采样糊掉，可调大 `requestImagePixelBudget` 和 `requestImageMaxBytes`。

## 守卫

`Doctor-SolidWorksDsh.ps1` 的 `Model image capability` 检查项会：

1. 用已安装 llm-pi-ai 适配器自身的 schema 校验 `~/.dsh/settings.yaml`；
2. 按运行时同一条解析链计算每个模型的有效输入能力；
3. 失败条件：自定义路由未显式声明 `defaultInput`，或 `agent-default-model` 解析结果不含 `image`。

手动运行（可独立于 Doctor）：

```powershell
node .\dsh-integration\scripts\Check-ModelImageCapability.mjs
```

退出码：`0` 通过，`1` 校验失败（输出 FAIL 原因），`2` 环境错误。

## 换模型 / 加模型 checklist

1. 新模型加在某自定义路由下：无需额外声明，默认可收图；纯文本模型加 `input: [ text ]`。
2. 新增自定义路由 provider：带 `defaultInput`（有视觉 `[ text, image ]`，纯文本 `[ text ]`）。
3. 切换默认模型：改 `agent-default-model` 后跑一次 Doctor，确认 `Model image capability` 为 True。
4. 改动 `settings.yaml` 后重启 `dsh web` 生效。
