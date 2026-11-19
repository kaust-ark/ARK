# 在 DeepSeek Harness 中使用 Idea2Paper

[English](README.md)

给 Idea2Paper 一个研究问题和新的项目目录。同一个 DSH 会话负责规划、执行实验、读取结果和写论文。独立评审会检查稿件，可调取原始数据和渲染后的论文页面。需要补实验或修改论点时，直接在同一项目里继续。

## 安装预发布版

本地实验需要 Linux、Node.js 22.12 或更高版本、pnpm、DSH 0.2.1-rc.2、含 venv 和 pip 的 Python 3.10 或更高版本、带 pdflatex 和 BibTeX 的 TeX Live，以及可用的 Docker daemon。首次运行会在独立缓存中安装随包分发的 Python wheel 及依赖，并构建实验镜像。请为首次下载和安装留出时间和磁盘空间。[DSH 安装指南](https://github.com/deepseek-ai/deepseek-harness/tree/dsh-v0.2.1-rc.2)

用这一条命令将 0.2.0-alpha.2 安装到专用 profile。使用 DSH Web Discover 需要上游带 Web profile 的完整 CLI。

```sh
dsh plugin --profile idea2paper add https://github.com/kaust-ark/ARK/releases/download/dsh-bundle-0.2.0-alpha.2/idea2paper-dsh-0.2.0-alpha.2.tgz
```

在源码仓库中，也可以先构建同一份安装包，再从本地安装。

```sh
python3 integrations/dsh/pack.py --out dist/dsh
dsh plugin --profile idea2paper add "$PWD/dist/dsh/idea2paper-dsh-0.2.0-alpha.2.tgz"
dsh --profile idea2paper --help
```

## 运行一个小实验

在 DSH 中配置模型。使用 DeepSeek provider 时，在启动 DSH 的环境中设置 `DEEPSEEK_API_KEY`。要检查论文中实际呈现的图，请使用支持图像输入的模型和 provider。独立运行时，配置了 Gemini key 的项目还可以调用 Google Deep Research、生成方法示意图，费用计入相应的 provider 账户。

```sh
dsh --profile idea2paper run search-study "在有序数组上比较线性查找和二分查找找不到目标时检查的元素个数。包含小数组，报告准确计数。"
dsh --profile idea2paper status search-study
dsh --profile idea2paper resume search-study "请报告测试范围内的交叉点和持平的情况。"
dsh --profile idea2paper stop search-study
dsh --profile idea2paper resume search-study
```

第一条命令新建项目。运行期间可从另一终端查看状态、提交指示或请求停止。中断后使用原 profile、模型和项目目录恢复。独立运行默认在本机执行实验，也支持已配置的 Slurm 环境。交付前须通过当前版本的独立评审及交付检查。

| 查看内容 | 项目文件 |
| --- | --- |
| 研究进展和对应证据路径 | `auto_research/state/research_events.jsonl` |
| 原始测量和实验图 | `results/`、`paper/figures/` |
| 论文 PDF | `paper/main.pdf` |
| 独立评审结论及问题证据 | `auto_research/state/native_review.json` |
| 受评版本的 PDF | `auto_research/state/reviews/<revision>/main.pdf` |
| 交付检查 | `auto_research/state/delivery_report.yaml` |

## 在 Discover 中查看

在 DSH Web 打开 **Plugins → More → Experimental plugins**，启用 **Custom plugin markets**，再选择 **Discover → Add index source**。发布后填入下方地址。插件卡片会显示精确的 Git 提交和兼容的 DSH 版本。索引和安装均属于当前 profile。如果 Web 使用单独的 profile，请用上面的 CLI 命令安装到 `idea2paper`。[DSH Discover 中文指南](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.1-rc.2/docs/user/guide/plugin-market.zh.md)

```text
https://raw.githubusercontent.com/kaust-ark/ARK/dsh-bundle-0.2.0-alpha.2/index.json
```

## 验证和发布

在源码仓库中准备好指定版本的 DSH 和 pnpm 后，构建并验证将要发布的同一份归档文件。分发仓库的输出目录须为新目录。

```sh
python3 integrations/dsh/pack.py --out dist/dsh
python3 integrations/dsh/verify.py dist/dsh/idea2paper-dsh-0.2.0-alpha.2.tgz --prepare dist/dsh/release
```

验证程序会从本地 HTTP 地址安装到干净的 profile，调用包内 Python 组件读取已停止的项目，再启动 DSH Web 核对 Discover 的 Git 安装目标，并通过本地 Git 镜像安装同一提交。生成的独立 Git 仓库包含归档中的文件和 wheel，索引指向实际的包提交。原 0.1.0-alpha.1 安装包可使用 `verify_legacy.py` 运行对应的测试。

通过发布检查后，维护者可按英文版的命令推送标签、上传已验证的归档，并核对公开下载文件的 SHA-256 和索引。具有 npm 发布权限的维护者还可将同一文件发布到 alpha 标签并核对 `dist.integrity`。卸载插件可运行 `dsh plugin --profile idea2paper remove idea2paper-dsh`。如需续跑，请保留项目目录和原 profile。[发布命令](README.md#verify-and-publish)
