<p align="center">
  <a href="README.md">English</a> &bull; <strong>中文</strong> &bull; <a href="README_ar.md">العربية</a>
</p>

<p align="center">
  <img src="https://idea2paper.org/assets/logo_ark_transparent.png" alt="idea2paper" width="260">
</p>

<h1 align="center">idea2paper</h1>

<p align="center">
  <em>减轻科研负担，掌舵科学方向。</em>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.10+-blue.svg" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/license-Apache%202.0-green.svg" alt="Apache 2.0">
  <a href="https://github.com/kaust-ark/ARK/actions/workflows/ci.yml"><img src="https://github.com/kaust-ark/ARK/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/agents-6-orange.svg" alt="6 Agents">
  <img src="https://img.shields.io/badge/venues-20+-purple.svg" alt="20+ Venues">
</p>

<p align="center">
  <a href="https://idea2paper.org/"><strong>官方网站</strong></a> &bull;
  <a href="#快速上手">快速上手</a> &bull;
  <a href="#环境要求">环境要求</a> &bull;
  <a href="#流水线">工作流</a> &bull;
  <a href="#智能体">智能体</a> &bull;
  <a href="#云端计算">云端计算</a> &bull;
  <a href="#cli-参考">命令行参考</a>
</p>

---

<!-- docs:start -->

idea2paper 协调 **6 个专业 AI 智能体**，将研究构想转化为完整论文 &mdash; 从方案分析、文献检索、Slurm 实验、LaTeX 撰写到迭代同行评审 &mdash; 同时通过 **命令行 (CLI)**、**仪表板 (Dashboard)** 或 **Telegram** 保持您的全程控制。

```
提供想法和目标会议，剩下的交给 idea2paper。
```

想最快上手，直接用托管实例 **[idea2paper.org](https://idea2paper.org/)**——邮箱登录，填一个 [OpenRouter](https://openrouter.ai/keys) API key 就能开跑。用 DeepSeek 这类平价模型跑完一整篇论文，通常只花 **约 $5 的自有 API 额度**。想自托管也只需一行命令安装（见下文）。

## 由 idea2paper 撰写的论文

<table align="center">
<tr>
<td align="center" width="33%">
<a href="https://idea2paper.org/assets/papers/marco.pdf"><img src="https://idea2paper.org/assets/paper-marco.png" alt="Budget-Constrained Multi-Modal Research Synthesis" width="320"></a>
<br>
<strong>Budget-Constrained Multi-Modal Research Synthesis via Iterative-Deepening Agentic Search</strong>
<br>
<sub>模板: EuroMLSys</sub>
</td>
<td align="center" width="33%">
<a href="https://idea2paper.org/assets/papers/heteroserve.pdf"><img src="https://idea2paper.org/assets/paper-heteroserve.png" alt="HeteroServe" width="320"></a>
<br>
<strong>HeteroServe: Capability-Weighted Batch Scheduling for Heterogeneous GPU Clusters in LLM Inference</strong>
<br>
<sub>模板: ICML</sub>
</td>
<td align="center" width="33%">
<a href="https://idea2paper.org/assets/papers/tierkv.pdf"><img src="https://idea2paper.org/assets/paper-tierkv.png" alt="TierKV" width="320"></a>
<br>
<strong>TierKV: Prefetch-Aware Memory Tiering for KV Cache in LLM Serving</strong>
<br>
<sub>模板: NeurIPS</sub>
</td>
</tr>
</table>

---

## 快速上手

```bash
curl -fsSL https://idea2paper.org/install.sh | bash
```

脚本会：

1. 检测系统、按需安装 miniforge、创建 `ark-base` 与 `ark` 两个 conda env、以可编辑模式将 idea2paper 装到 `~/ARK`，并安装 OpenHands CLI（智能体运行时，经 `uv` 安装——自带独立的 Python 3.12）。
2. 询问你的 API key 和**仪表板登录邮箱**。推荐只配一个 **OpenRouter** key——它解锁全部模型，外加深度研究和图表生成；直接填 Anthropic / OpenAI / Gemini / DeepSeek 的官方 key 也可以。回车跳过任意一项。
3. 把仪表板装成 `systemd --user` 服务（端口 `9527`，用 `--no-webapp` 退出）。
4. 打印一个一次性的 **magic-link URL** 到你输入的邮箱地址——点一次链接就登录本地仪表板。**不需要 SMTP，也不需要 Google OAuth**。

之后 <http://localhost:9527> 仪表板就是主要的交互界面——建项目、选模型、运行、监控。CLI 也照常可用：

```bash
ark doctor          # 验证安装
ark new myproject   # 交互式项目向导
ark run  myproject
ark monitor myproject
```

随时跑 `ark webapp login <email>` 获取新的登录链接。完整脚本参数见 [`website/homepage/install.sh --help`](website/homepage/install.sh)。

### 从现有 PDF 开始

```bash
ark new myproject --from-pdf proposal.pdf
```

idea2paper 通过 PyMuPDF + Claude Haiku 解析 PDF，自动填写向导信息，并根据提取的规格开始工作。

---

## 环境要求

- **Python 3.10+** (需安装 `pyyaml` 和 `PyMuPDF`)
- **智能体运行时**：[OpenHands CLI](https://github.com/OpenHands/OpenHands-CLI)（经 `uv` 安装，自带 Python 3.12）&mdash; 一个运行时即可驱动 Claude / GPT / Gemini / 任何 [LiteLLM](https://docs.litellm.ai/docs/providers) 支持的模型，通过 `config.yaml` 里的 `model` 字段按项目选择。
- **API key**：一个 [OpenRouter](https://openrouter.ai/keys) key 即可覆盖全部模型、深度研究（Perplexity）和 AI 绘图；单一厂商的 key（Anthropic / OpenAI / Gemini / DeepSeek 等）也能用，只是会缺少该厂商不提供的那部分能力。
- **可选**: LaTeX (`pdflatex` + `bibtex`)、Slurm。

### 安装步骤

最简单的方式是 [快速上手](#快速上手) 中的一键安装脚本，它会替你执行下面这些步骤并打印上手提示。手动安装如下：

```bash
# 1. 创建项目研究栈模板（这里不要装 idea2paper —— 每个新项目都会克隆此环境，
#    所以必须保持纯净）。
conda env create -f environment.yml         # Linux 系统 (创建 "ark-base")
# 或 macOS 系统:
conda env create -f environment-macos.yml   # macOS 系统 (创建 "ark-base")

# 2. 把 idea2paper 本体装进一个独立的 env（不要装进 ark-base）。
conda create -n ark python=3.11 -y
conda activate ark
pip install -e .                    # 核心库
pip install -e ".[research]"       # + Gemini 深度研究与 Nano Banana
pip install -e ".[webapp]"         # + 仪表板 / systemd 服务支持

# 3. 安装 OpenHands CLI（智能体运行时）。它是一个独立的 `uv` 工具，
#    自带专属的 Python 3.12——不是 pip 依赖——且必须位于 PATH 上，
#    编排器子进程才能找到它。
pip install uv && uv tool install --python 3.12 openhands

# 4. 验证（检查 openhands 运行时是否在 PATH 上、密钥是否就位等）
ark doctor
```

---

## 架构

<p align="center">
  <img src="assets/framework.png" alt="idea2paper framework" width="900">
</p>

idea2paper 协调三个阶段 &mdash; **初始化与研究**、**迭代开发** 和 **迭代评审** &mdash; 通过共享记忆、在每次智能体调用时重新注入的 **Goal Anchor**（防止跨迭代漂移），以及经 Web 仪表板或 Telegram 的人机协同来协同工作。

---

## 流水线

idea2paper 按顺序运行三个阶段。评审阶段会循环进行，直到论文达到目标分数。

| 阶段 | 过程内容 |
|:------|:-------------|
| **研究** | 5步流水线：设置 (conda 环境) &rarr; 分析方案 (researcher) &rarr; 深度研究 (Perplexity via OpenRouter，或 Gemini) &rarr; 专项化 (researcher) &rarr; 引导 (技能与引用) |
| **开发** | 迭代实验循环：规划实验 &rarr; 运行实验 (Slurm 或本地) &rarr; 分析结果 &rarr; 评估完成度 |
| **评审** | 编译 &rarr; 评审 &rarr; 计划 &rarr; 执行 &rarr; 验证，循环直到得分 &ge; 阈值 |

### 评审循环

评审阶段的每次迭代包含 **5 个步骤**：

| 步骤 | 描述 |
|:-----|:------------|
| **编译** | LaTeX &rarr; PDF，计算页数，生成页面图像 |
| **评审** | AI 评审员评分 (1-10)，列出主要和次要问题 |
| **计划** | 规划器 (Planner) 创建优先处理的任务计划 |
| **执行** | 研究员与实验员并行工作；撰写员修改 LaTeX |
| **验证** | 验证修改是否可编译；重新生成 PDF |

循环将重复进行，直到分数达到录取阈值 &mdash; 或者您通过 Telegram 进行人工干预。

---

## 智能体

| 智能体 | 职责 |
|:------|:-----|
| **研究员** | 分析方案；执行深度研究文献调研；为项目定制智能体提示词 |
| **评审员** | 根据会议标准为论文评分，生成改进任务 |
| **规划器** | 将评审反馈转化为优先行动计划；分析开发阶段的结果 |
| **撰写员** | 撰写和精炼 LaTeX 章节，并附带经过 DBLP 验证的参考文献 |
| **实验员** | 设计实验，提交 Slurm 任务，分析实验结果 |
| **编程员** | 编写和调试实验代码及分析脚本 |

---

## 护栏与步骤日志

自主运行全程受监督、有关卡，不是黑箱。

- **实时步骤日志。** 每个智能体的动作——每条 bash 命令、每次文件编辑及其结果——都会实时写入日志（不再有 30 分钟的空白），并同时落入结构化的 `agent_steps.jsonl`。密钥等敏感值会被自动打码。用 `log_verbosity: quiet|normal|verbose|debug` 调整详细程度。
- **高风险操作先停下来问你。** 在删除文件、批量提交任务、开通付费云实例、处理凭据、推送/外传数据或触及花费上限之前，idea2paper 会通过 **Telegram** 请求你的批准（同意 / 拒绝 / 记住本次选择）并等待答复。它会记住你的回答避免重复打扰，超时视为拒绝；未配置 Telegram 时则**放行并记录**——所以永远不会卡死。
- **想法把关（Gate A / Gate B）。** 每个提交的想法都要先过一道启动前的伦理与合理性审查（Gate A）；文献调研之后再做一次新颖性/范围检查（Gate B），标记与已有工作的重叠，把实事求是的定位写进论文，而不是夸大其词。
- **交付检查。** 论文交付前会对照一份明确的契约逐项核验——AI 使用声明齐备、所有引用均已解析、参考文献非空、生成的图确实被用上、页数符合预算。同样的检查也可以用 `ark audit <project> [--repair]` 单独运行。
- **AI 使用声明。** 每篇论文都自动附带一段致谢，说明它由 Idea2Paper 生成并经作者审阅——对所有会议一视同仁。
- **两层强制机制。** Shadow-PATH 包装器在危险命令*运行前*拦截；熔断器兜底任何绕过它的行为。编排器自身的云开通 / git push / 花费动作同样受管控——但你亲手触发的命令（`ark clear`、删除、停止）不受限制。

以上都在 [`config.example.yaml`](config.example.yaml) 的 `intervention:` 块中配置；默认的自主级别（`standard`）只在真正高风险的操作上打断你。

---

## idea2paper 的独特之处

| | 其他工具 | idea2paper |
|---|:------------|:----|
| **控制力** | 完全自主 &mdash; 容易偏离意图，无法中途纠正 | 人机协同：在关键决策点暂停，通过 Telegram 或 Web 引导 |
| **排版** | 布局损坏、LaTeX 错误、需要手动清理 | 会议模板 + 亚页级长度控制，精准卡住页数上限 |
| **引用** | LLM 编造看似真实的虚假参考文献 | API 优先生成 BibTeX (DBLP / CrossRef / arXiv)，并做内容&ndash;论断对齐 |
| **评审** | 仅对 LaTeX 源码做纯文本评审 | 视觉接地：页面图像 **加** 源码，按会议标准打分 |
| **插图** | 默认样式、尺寸错误、缺乏页面意识 | AI 概念图 (PaperBanana) + 面向会议的尺寸控制：图片按印刷尺寸保存，从不放大 |
| **隔离性** | 共享环境 &mdash; 项目之间相互干扰 | 每个项目独立的 conda 环境、沙盒化 HOME 目录、完整的隔离 |
| **真实性** | LLM 模拟结果而非运行真实实验 | 防模拟提示词 + 内置技能强制执行真实运行 |

---

## 环境隔离

每个项目都在其独立的 **conda 环境**中运行。这确保了完整的隔离：

- **隔离的 Python** &mdash; 每个项目拥有独立的 `.env/` 目录和包。
- **隔离的 HOME 目录** &mdash; 每个编排器运行时将 `HOME` 设置为项目目录。
- **无交叉污染** &mdash; `PYTHONNOUSERSITE=1` 防止泄露全局用户包。
- **自动配置** &mdash; `ark run` 和 Web 门户会自动检测并使用项目的 conda 环境；如果缺失，流水线将引导安装。

```bash
# conda 环境在第一次运行时自动创建。
# ark run 将检测并使用它：
ark run myproject
#   Conda env: /path/to/projects/myproject/.env
```

## 技能系统

idea2paper 附带 **内置技能** &mdash; 智能体在运行时加载的模块化指令集，用于强制执行最佳实践：

| 技能 | 目的 |
|:------|:--------|
| **研究真实性** | 防模拟提示词：智能体必须运行真实实验，不得编造输出 |
| **人工干预** | 升级协议：智能体在执行不可逆操作前会通过 Telegram 询问 |
| **环境隔离** | 强制执行每个项目的环境边界 |
| **运行时沙箱** | 在运行时把每个项目锁定在自己的 conda 环境、`HOME` 与临时目录中 |
| **插图真实性** | 验证插图内容与数据匹配；防止占位符或幻觉图表 |
| **页面调整** | 通过调整内容密度而非删除章节来维持页面限制 |

内置技能位于 `skills/builtin/`，在流水线引导期间自动安装。领域技能（例如 HPC）位于 `skills/library/`，由研究员在初始化阶段按需取用。

---

## CLI 参考

| 命令 | 描述 |
|:--------|:------------|
| `ark new <name>` | 通过交互式向导创建项目 |
| `ark run <name>` | 启动流水线 (自动检测项目环境) |
| `ark status [name]` | 得分、迭代次数、阶段、成本 |
| `ark monitor <name>` | 实时监控：智能体活动、得分趋势 |
| `ark update <name>` | 注入中途指令 |
| `ark stop <name>` | 优雅停止 |
| `ark restart <name>` | 停止并重启 |
| `ark research <name>` | 独立运行 Gemini 深度研究 |
| `ark config <name> [key] [val]` | 查看或编辑配置 |
| `ark clear <name>` | 重置状态以重新开始 |
| `ark delete <name>` | 完全删除项目 |
| `ark setup-bot` | 配置 Telegram 机器人 |
| `ark list` | 列出所有项目及其状态 |
| `ark doctor` | 自托管安装诊断（环境、API key、Web 服务） |
| `ark cite-check <name>` | 用 DBLP / CrossRef 校验项目引用 |
| `ark cite-search <query>` | 搜索学术数据库 |
| `ark audit <dir> [--repair]` | 按交付契约核验已交付的论文 |
| `ark share create <name>` | 为项目生成分享链接 |
| `ark webapp install` | 安装 Web 仪表板服务 |
| `ark access …` | 管理（可选的）Cloudflare Access 允许列表 |

---

## 仪表板 (Dashboard)

idea2paper 包含一个基于 Web 的仪表板，用于管理项目、查看分数和引导智能体——由单个 FastAPI 进程提供服务，主页也由它托管（一个端口，一个 systemd 单元）。除了实时阶段状态和日志，它还提供：

- **公平排队** &mdash; 每次启动最多跑 2 轮开发 + 2 轮评审迭代；通道占满时，新项目进入队列，显示排位和**预计完成时间**，论文完成后你会收到邮件通知。
- **诚实的账单** &mdash; 成本卡片优先展示**按供应商实际计费**的总额（真实的 OpenRouter 账单，含深度研究和绘图开销）；拿不到实际账单时才显示估算值，并明确标注。
- **按 key 过滤的模型选择器** &mdash; 只有你持有对应 key 的模型才可选；一个 OpenRouter key 解锁整个列表。
- **与论文对话** &mdash; 就已完成的项目提问、要求定向修改，或单独重跑某个实验，无需完整迭代。
- **页面适配模式** &mdash; Relaxed / Balanced / Strict 三档，控制论文向会议页数限制压缩的力度。

### 配置

通过 `.ark/webapp.env` 配置（首次运行 `ark webapp` 时自动创建）。设置 `SMTP_*` 启用魔术链接登录，用 `ALLOWED_EMAILS` / `EMAIL_DOMAINS` 限制访问，可选设置 `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` 启用 Google OAuth。

### 管理命令

| 命令 | 描述 |
|:--------|:------------|
| `ark webapp` | 在前台启动仪表板 (对调试很有用)。 |
| `ark webapp release` | 标记当前代码并部署到生产工作树。 |
| `ark webapp install [--dev]` | 作为 `systemd` 用户服务安装并启动。 |
| `ark webapp status` | 显示 systemd 服务的状态。 |
| `ark webapp restart` | 重启仪表板服务。 |
| `ark webapp logs [-f]` | 查看或跟踪服务日志。 |
| `ark webapp login <email>` | 打印一个新的 magic-link 登录链接。 |
| `ark webapp publish` | 把 origin/main 打上标签作为下一个发布版本（标签驱动部署）。 |

<details>
<summary><strong>服务详情 (生产 vs 开发)</strong></summary>

| | 生产 (Prod) | 开发 (Dev) |
|---|:-----|:----|
| **端口** | 9527 | 1027 |
| **服务名称** | `ark-webapp` | `ark-webapp-dev` |
| **Conda 环境** | `ark-prod` | `ark-dev` |
| **代码源** | `~/.ark/prod/` (已固定) | 当前存储库 (实时) |

</details>

### 为他人托管 Idea2Paper？

搭建一个**托管的、多租户**的 Idea2Paper 实例——主机与 Web 应用配置、共享 prod 的团队发布，以及让客户在各自云账号中运行云端计算的 GCP/AWS 启动器设置——在运维手册中有完整的分步说明（英文）：

**→ [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**

<details>
<summary><strong>直接调用编排器</strong></summary>

```bash
python -m ark.orchestrator --project myproject --mode paper --max-iterations 20
python -m ark.orchestrator --project myproject --mode dev
```

</details>

---

## Docker 使用

### 架构要求

> [!IMPORTANT]
> idea2paper 研究运行时依赖的科学库在 x86_64 上最稳定。如果您在 **Apple Silicon (M1/M2/M3)** Mac 上构建，必须为 `linux/amd64` 平台构建。
>
> 所有的 idea2paper Dockerfile 和 `docker-compose.yml` 默认都配置为强制使用 `linux/amd64`。

### 使用 Docker Compose 运行

运行 idea2paper Web 门户最简单的方法是使用 `docker-compose`。在项目根目录下：

```bash
# 启动 Web 门户 (自动为 amd64 构建镜像)
docker compose -f docker/docker-compose.yml up --build -d
```

Web 门户可以通过 `http://localhost:9527` 访问。所有数据库、配置和项目数据都自动持久化在 Docker 命名卷 (`ark_data`) 中。

查看 Web 门户的实时日志：
```bash
docker compose -f docker/docker-compose.yml logs -f webapp
```

### 推送到 Google Cloud Platform (GCP)

idea2paper 包含一个构建并推送镜像到 Google Artifact Registry 或 GCR 的脚本。

```bash
# 推送到 Artifact Registry (推荐)
./docker/push-gcp.sh --project [PROJECT_ID] --region [REGION] --repo [REPO] --build

# 推送到旧版 Container Registry (gcr.io)
./docker/push-gcp.sh --project [PROJECT_ID] --legacy --build
```
即使在 macOS 上运行，`--build` 标志也会自动为 `linux/amd64` 构建镜像。

### 配置

自定义 Web 门户配置 (例如，设置用于魔术链接登录的 SMTP 或 OAuth)：

```bash
# 创建自定义配置
cp .ark/webapp.env.example .ark/webapp.env
# 编辑 .ark/webapp.env 以填写您的凭据
```
然后取消注释 `docker/docker-compose.yml` 中 `webapp` 服务下的环境卷映射：
```yaml
      - ../.ark/webapp.env:/data/.ark/webapp.env:ro
```

### 运行单个作业

您可以使用 idea2paper 作业容器与 Web 应用程序一起运行隔离的研究作业。取消注释 `docker/docker-compose.yml` 中的 `job` 服务，然后运行：

```bash
docker compose -f docker/docker-compose.yml run --rm job \
  --project myproject \
  --project-dir /data/projects/<user-id>/myproject \
  --mode research \
  --iterations 10
```

*注意：您必须将所需的 API 密钥 (例如 `ANTHROPIC_API_KEY`、`GEMINI_API_KEY`) 作为环境变量传递。*

---

## 云端计算 (Cloud Compute)

idea2paper 的 **v2 云端架构**将*控制平面 (Control Plane)* 与*执行平面 (Execution Plane)* 解耦，使完整的编排器能够运行在 **SkyPilot 预配置的集群**上，而您只需通过一个轻量级的本地 Web 应用进行交互。[SkyPilot](https://docs.skypilot.co) 现在是唯一的云端计算路径 &mdash; 它通过单一抽象层跨 AWS/GCP/Azure/Kubernetes 进行预配置，内置抢占式实例 (spot)、重试以及 autostop 自动销毁机制。

**工作原理：**
1. 本地 Web 应用（或 CLI）充当轻量级启动器 &mdash; 它运行 `sky launch` 来预配置远程**编排器集群 (Orchestrator cluster)**，通过 SkyPilot 的 `workdir`/`file_mounts` 同步您的项目代码与 API 密钥，并触发编排器进程。
2. **编排器集群**在一个分离的会话 (detached session) 中远程运行所有高层逻辑（研究员、规划器、写作者、LaTeX、图表）。
3. 实验既可以运行在同一个编排器集群上，也可以运行在一个独立的、由 SkyPilot 预配置的 GPU 集群上（可独立配置）。
4. 编排器通过 `/v1` 控制平面 API 向本地回报状态；Web 应用则流式传输日志并刷新仪表板。运行完成后（或在空闲一段时间后），集群通过 **autostop** 自动终止。

> [!TIP]
> 云端凭据使用您的 `SECRET_KEY` 在静态时加密。您的密钥绝不会被记录或传输给第三方。

<details>
<summary><strong>配置层级</strong></summary>

idea2paper 为云端计算使用三层配置模型：
1. **系统默认值**: 在 `webapp.env` 中设置（持有已烘焙 ARK 镜像的中央 `CLOUD_GCP_PROJECT`，以及 `CLOUD_LAUNCHER_SA`、`CLOUD_LAUNCHER_SA_KEY` 和 `CLOUD_CONDA_ENV`）。
2. **全局用户默认值**: 在**设置**面板 (⚙️) 中设置。这些适用于您的所有项目。
3. **项目覆盖**: 在项目创建或重启期间设置。这些具有最高优先级。

这种层级结构允许您只需定义一次标准默认值，同时可以轻松地为特定的实验切换到强大的 GPU 实例（加速器、抢占式）。

</details>

---

### 通过仪表板启用云端计算

1. 打开**设置**面板 (顶部导航栏中的 ⚙️ 图标)。
2. 打开 **Compute** 标签页。
3. 输入您的 **GCP Project ID**，为显示的 `ark-launcher` 服务账号授予您项目上所需的角色，然后点击 **Verify access**。（不会上传任何服务账号密钥 &mdash; 您通过 IAM 将访问权限委托给您自己的项目。运维者请参见 [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) §4。）
4. 点击**保存**。

创建新项目时，您现在可以独立选择：
- **编排器后端 (Orchestrator Backend)** &mdash; `skypilot` 表示在 SkyPilot 预配置的集群上运行控制平面，`local` 表示在与 Web 应用相同的机器上运行。
- **实验后端 (Experiment Backend)** &mdash; `skypilot` 用于 GPU 实验，`local` 表示在编排器集群本身上运行实验。

---

<details>
<summary><strong>创建项目</strong></summary>

配置好云端计算后，通过仪表板启动项目：

1. 从仪表板主页点击 **New Project**。
2. 填写研究目标、目标会议和任何附加说明。
3. 点击 **Submit** &mdash; Web 应用会生成 `config.yaml`，通过 SkyPilot 预配置编排器集群，同步您的项目，并启动运行。

生成的 `config.yaml` 存储在：

```
~/.ark/data/projects/<user_id>/<project_id>/config.yaml
```

您可以随时检查或手动编辑此文件（例如，调整实例类型或添加 `setup_commands`）。更改在下次运行或重启时生效。

> [!NOTE]
> 如果在您的 `.ark/webapp.env` 中设置了 `PROJECTS_ROOT`，上述路径将被替换为 `$PROJECTS_ROOT/<user_id>/<project_id>/config.yaml`。

</details>

---

### 云端提供商设置

配置 GCP / AWS / SkyPilot 启动器——构建预烘焙镜像、创建中央 `ark-launcher` 身份、写入 `webapp.env`——是每个托管实例只需做一次的**运维**任务。它按云分步记录在部署指南中（英文）：

**→ [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** — §4 GCP · §5 AWS · §6 客户接入 · §8.4 `config.yaml` 参考。

如果您只是*使用*托管实例，则无需运行上述任何步骤——只需打开 **Settings → Compute**，输入您的 GCP 项目 / AWS 账号，然后点击 **Verify access**（见上）。在 **Azure 或 Kubernetes** 上运行？SkyPilot 同样支持；请按 [SkyPilot 文档](https://docs.skypilot.co) 配置该云的凭据。

---

<details>
<summary><strong>日志流式传输与重新连接</strong></summary>

- **日志流式传输** &mdash; 编排器集群通过 `/v1` 控制平面 API 向本地回传日志；Web 应用定期轮询以显示实时进度。
- **状态同步** &mdash; 编排器定期将其 `auto_research/` 状态检查点保存到控制平面，因此仪表板 UI 保持最新，且运行可在 VM 丢失后存续。
- **重新连接** &mdash; 如果您重启本地 Web 应用，idea2paper 会检测到持久化的 SkyPilot 集群并重新连接到正在运行的进程，无需重新预配置。

</details>

<details>
<summary><strong>成本控制</strong></summary>

> [!WARNING]
> 云端集群按小时计费。idea2paper 依赖 SkyPilot 内置的销毁机制来防止成本失控：
>
> - **Autostop-down** &mdash; 每个集群在启动时都带有一个 autostop-down 时间窗；若空闲超过该时间窗，它会**自我终止**。实验集群*始终* autostop-down（只能调整，不能禁用）；编排器集群默认设有一个时间窗作为崩溃安全网（可通过 `idle_minutes_to_autostop` 调整）。
> - **手动停止** &mdash; 在仪表板中点击 **Stop** 会刷新结果并销毁集群 (`sky down`)。
>
> 如果 Web 应用进程被意外杀死，autostop-down 时间窗仍会自行终止集群。在意外关闭后，请务必确认没有残留集群 (`sky status`)。

</details>

---

## Telegram 集成

```bash
ark setup-bot    # 一次性操作：粘贴 BotFather 令牌，自动检测聊天 ID
```

您将获得：
- **丰富通知** &mdash; 格式化的分数变化、阶段转换、智能体活动和错误
- **发送指令** &mdash; 实时指导当前迭代
- **请求 PDF** &mdash; 将最新编译的论文发送到聊天
- **人工干预** &mdash; 智能体在执行不可逆操作前会向您请示
- **HPC 友好** &mdash; 处理企业/HPC 网络上的自签名 SSL 证书

---

## 支持的会议

随仓库提供的 LaTeX 模板：**NeurIPS、ICML、ICLR、AAAI、MLSys**，以及 **ACL** 族（ACL / EMNLP / NAACL / EACL / AACL / COLING）、**CVF** 族（CVPR / ICCV / WACV）、**ACM acmart** 族（SOSP / EuroSys / ASPLOS / EuroMLSys）、**USENIX** 族（OSDI / NSDI / ATC / FAST / Security）、**IEEE / IEEEtran**（INFOCOM 及其他 IEEE 会议）——全部取自官方 2026 样式文件（MLSys 沿用其未变的 2025 套件）——另含通用 **article** 兜底模板（TMLR、Workshop、技术报告）。同样接受自定义模板 &mdash; idea2paper 会扫描 `.tex` / `.aux` / `.sty` 学习排版、修编译错误、并精确控制页数。

## 社区

<p align="center">
  <strong>微信交流群 / Join our WeChat group</strong><br>
  <img src="assets/wechat_qr.jpg" alt="WeChat group: Idea2Paper" width="240"><br>
  <sub>微信群二维码会定期更新；若已过期，请提 issue 联系我们。</sub>
</p>

## 许可证

[Apache 2.0](LICENSE)
