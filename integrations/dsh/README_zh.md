# 在 DeepSeek Harness 中使用 Idea2Paper

[English](README.md)

给 Idea2Paper 一个研究问题和新的项目目录。同一个 DSH 会话负责规划、执行实验、读取结果和写论文。独立评审会检查稿件，可调取原始数据和渲染后的论文页面。需要补实验或修改论点时，直接在同一项目里继续。

## 安装预发布版

此源码构建支持 Linux 和 pnpm 下的两个 DSH 版本。DSH 0.2.1-rc.2 需要 Node.js 22.12 或更高版本，DSH 0.2.2-rc.5 需要 Node.js 24.9 或更高版本。续接项目时请使用创建项目的 DSH 版本和 profile。[DSH 0.2.1-rc.2](https://www.npmjs.com/package/@deepseek-ai/dsh/v/0.2.1-rc.2) · [DSH 0.2.2-rc.5](https://www.npmjs.com/package/@deepseek-ai/dsh/v/0.2.2-rc.5)

本地实验还需要含 venv 和 pip 的 Python 3.10 或更高版本、带 pdflatex 和 BibTeX 的 TeX Live，以及可用的 Docker daemon。首次运行会在独立缓存中安装随包分发的 Python wheel 及依赖，并构建实验镜像。请为首次下载和安装留出时间和磁盘空间。

用这一条命令将 0.2.0-alpha.17 安装到专用 profile。使用 DSH Web Discover 需要上游带 Web profile 的完整 CLI。

```sh
dsh plugin --profile idea2paper add https://github.com/kaust-ark/ARK/releases/download/dsh-bundle-0.2.0-alpha.17/idea2paper-dsh-0.2.0-alpha.17.tgz
```

如需安装本地构建的版本，可在仓库根目录运行以下命令。

```sh
python3 integrations/dsh/pack.py --out dist/dsh
version=$(node -p "require('./integrations/dsh/package.json').version")
dsh plugin --profile idea2paper add "$PWD/dist/dsh/idea2paper-dsh-$version.tgz"
dsh --profile idea2paper --help
```

离线示例包含三个版本的论文 PDF、各版的评审意见和测量数据。解压后即可对照查看同一问题如何在修订中得到解决，无需模型密钥。这份示例首次随 alpha.3 发布，本版沿用原始记录。[下载离线评审示例](https://github.com/kaust-ark/ARK/releases/download/dsh-bundle-0.2.0-alpha.17/review-replay.zip)

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

研究 agent 可直接用测量 JSON 的字段画折线图、点图和分组柱状图。自定义绘图脚本读取 `/study/results.json`，并把图存入 `/output/`。对照实验默认由 Nano Banana 绘制研究对象，再按已确认的实验方案排版各组条件。通过核查的图会返回对应图注，也可以按需绘制机制图。修订时可以直接编辑已读过的行，已有成功编译记录的稿件会在保存修改前由 TeX 试编译。

### 独立评审

对于有已核验测量表和结论段的论文，评审可以在读稿前记录比较对象、其他可能解释，以及能区分这些解释的观测。它可以在最终 PDF 中搜索短语，对照不同章节的表述。在托管版项目页展开评审判断，即可查看证据，并就引用的具体页面向研究者提问。

在一次四页论文实测中，评审曾接受一条原始测量不足以证实的机制归因。使用科学结论前，请核对评审引用的证据。

修改实验后，如果实测数据仍与上一版相同，reviewer 会先看原始研究任务、实验条件字段和指标名，并为一个真实条件编写独立复算程序。Bundle 锁定程序后，同一评审会话才会看到论文、作者的代码和结果。评审记录逐项保留复算值和实测值。如果缺少数据或算力，评审会标明缺失的证据。

在仪表盘展开「评审检查了什么 → 实验控制项」可以查看比较结果。复算在本地 Docker 或配置的 Apptainer 科学镜像中离线运行，Slurm 项目无需为此再提交作业。

## 在 Discover 中查看

在 DSH Web 打开 **Plugins → More → Experimental plugins**，启用 **Custom plugin markets**，再选择 **Discover → Add index source**。填入下方的 0.2.0-alpha.17 索引地址。插件卡片会显示精确的 Git 提交和兼容的 DSH 版本。索引和安装均属于当前 profile。如果 Web 使用单独的 profile，请用上面的 CLI 命令安装到 `idea2paper`。[DSH Discover 中文指南](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.1-rc.2/docs/user/guide/plugin-market.zh.md)

```text
https://raw.githubusercontent.com/kaust-ark/ARK/dsh-bundle-0.2.0-alpha.17/index.json
```

## 验证和发布

在源码仓库中准备好指定版本的 DSH 和 pnpm 后，构建并验证将要发布的同一份归档文件。分发仓库的输出目录须为新目录。

```sh
python3 integrations/dsh/pack.py --out dist/dsh
version=$(node -p "require('./integrations/dsh/package.json').version")
python3 integrations/dsh/verify.py "dist/dsh/idea2paper-dsh-$version.tgz" --prepare dist/dsh/release
```

验证程序会从本地 HTTP 地址安装到干净的 profile，调用包内 Python 组件读取已停止的项目，再启动 DSH Web 核对 Discover 的 Git 安装目标，并通过本地 Git 镜像安装同一提交。生成的独立 Git 仓库包含归档中的文件和 wheel，索引指向实际的包提交。原 0.1.0-alpha.1 安装包可使用 `verify_legacy.py` 运行对应的测试。

通过发布检查后，维护者可按英文版的命令推送标签，一并上传已验证的归档、alpha.3 的离线评审示例和为这两份文件生成的 SHA256SUMS，再核对公开下载和索引，并在全新的 DSH 环境中验证安装。具有 npm 发布权限的维护者还可将同一文件发布到 alpha 标签并核对 `dist.integrity`。卸载插件可运行 `dsh plugin --profile idea2paper remove idea2paper-dsh`。如需续跑，请保留项目目录和原 profile。[发布命令](README.md#verify-and-publish)
