<p align="center">
  <a href="README.md">English</a> &bull; <strong>中文</strong> &bull; <a href="README_ar.md">العربية</a>
</p>

<p align="center">
  <img src="https://idea2paper.org/assets/logo_ark_transparent.png" alt="ARK" width="260">
</p>

<h1 align="center">ARK &mdash; 自动化研究工具包 (Automatic Research Kit)</h1>

<p align="center">
  <em>减轻科研负担，掌舵科学方向。</em>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.9+-blue.svg" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/license-Apache%202.0-green.svg" alt="Apache 2.0">
  <a href="https://github.com/kaust-ark/ARK/actions/workflows/ci.yml"><img src="https://github.com/kaust-ark/ARK/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/agents-6-orange.svg" alt="6 Agents">
  <img src="https://img.shields.io/badge/venues-11+-purple.svg" alt="11+ Venues">
  <img src="https://img.shields.io/badge/tests-225-brightgreen.svg" alt="225 Tests">
</p>

<p align="center">
  <a href="https://idea2paper.org/"><strong>官方网站</strong></a> &bull;
  <a href="#快速上手">快速上手</a> &bull;
  <a href="#环境要求">环境要求</a> &bull;
  <a href="#ark-流水线">工作流</a> &bull;
  <a href="#ark-智能体">智能体</a> &bull;
  <a href="#云端计算">云端计算</a> &bull;
  <a href="#cli-参考">命令行参考</a>
</p>

---

ARK 协调 **6 个专业 AI 智能体**，将研究构想转化为完整论文 &mdash; 从方案分析、文献检索、Slurm 实验、LaTeX 撰写到迭代同行评审 &mdash; 同时通过 **命令行 (CLI)**、**仪表板 (Dashboard)** 或 **Telegram** 保持您的全程控制。

```
提供想法和目标会议，剩下的交给 ARK。
```

## 由 ARK 撰写的论文

<table align="center">
<tr>
<td align="center" width="50%">
<a href="https://idea2paper.org/assets/papers/marco.pdf"><img src="https://idea2paper.org/assets/paper-marco.png" alt="MARCO" width="320"></a>
<br>
<strong>MARCO: Budget-Constrained Multi-Modal Research Synthesis via Iterative-Deepening Agentic Search</strong>
<br>
<sub>模板: EuroMLSys</sub>
</td>
<td align="center" width="50%">
<a href="https://idea2paper.org/assets/papers/heteroserve.pdf"><img src="https://idea2paper.org/assets/paper-heteroserve.png" alt="HeteroServe" width="320"></a>
<br>
<strong>HeteroServe: Capability-Weighted Batch Scheduling for Heterogeneous GPU Clusters in LLM Inference</strong>
<br>
<sub>模板: ICML</sub>
</td>
</tr>
<tr>
<td align="center" width="50%">
<a href="https://idea2paper.org/assets/papers/tierkv.pdf"><img src="https://idea2paper.org/assets/paper-tierkv.png" alt="TierKV" width="320"></a>
<br>
<strong>TierKV: Prefetch-Aware Memory Tiering for KV Cache in LLM Serving</strong>
<br>
<sub>模板: NeurIPS</sub>
</td>
<td align="center" width="50%">
<a href="https://idea2paper.org/assets/papers/gac.pdf"><img src="https://idea2paper.org/assets/paper-gac.png" alt="GAC" width="320"></a>
<br>
<strong>Why Smaller Is Slower: Dimensional Misalignment in Compressed Large Language Models</strong>
<br>
<sub>模板: ICLR</sub>
</td>
</tr>
</table>

---

## 快速上手

```bash
# 一键自托管安装 (Linux / macOS)
curl -fsSL https://idea2paper.org/install.sh | bash

# 验证安装
ark doctor

# 创建项目 (交互式向导)
ark new mma

# 运行 — ARK 将从这里接管
ark run mma

# 实时监控 / 查看进度
ark monitor mma
ark status  mma
```

安装脚本会检测系统、按需安装 miniforge、创建 `ark-base` 与 `ark` 两个 conda env，并以可编辑模式将 ARK 装到 `~/ARK`。加 `--webapp` 还可顺带把仪表板装成 `systemd --user` 服务（端口 9527）。完整脚本见 [`website/homepage/install.sh`](website/homepage/install.sh)。

向导将引导您完成：代码目录、目标会议、研究构想、作者信息、计算后端、图表生成和 Telegram 设置。

### 从现有 PDF 开始

```bash
ark new mma --from-pdf proposal.pdf
```

ARK 通过 PyMuPDF + Claude Haiku 解析 PDF，自动填写向导信息，并根据提取的规格开始工作。

---

## 环境要求

- **Python 3.9+** (需安装 `pyyaml` 和 `PyMuPDF`)
- **智能体命令行**: 推荐 [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (推荐使用 Claude Max 订阅) **或** [Gemini CLI](https://github.com/google-gemini/gemini-cli) &mdash; 可按项目选择。
- **可选**: LaTeX (`pdflatex` + `bibtex`)、Slurm、用于 AI 绘图的 `google-genai`。

### 安装步骤

最简单的方式是 [快速上手](#快速上手) 中的一键安装脚本，它会替你执行下面这些步骤。手动安装如下：

```bash
# 1. 创建项目研究栈模板（这里不要装 ARK —— 每个新项目都会克隆此环境，
#    所以必须保持纯净）。
conda env create -f environment.yml         # Linux 系统 (创建 "ark-base")
# 或 macOS 系统:
conda env create -f environment-macos.yml   # macOS 系统 (创建 "ark-base")

# 2. 把 ARK 本体装进一个独立的 env（不要装进 ark-base）。
conda create -n ark python=3.11 -y
conda activate ark
pip install -e .                    # 核心库
pip install -e ".[research]"       # + Gemini 深度研究与 Nano Banana
pip install -e ".[webapp]"         # + 仪表板 / systemd 服务支持

# 3. 验证
ark doctor
```

---

## ARK 架构

<p align="center">
  <img src="assets/framework.png" alt="ARK Framework" width="900">
</p>

ARK 协调三个阶段 &mdash; **初始化与研究**、**迭代开发** 和 **迭代评审** &mdash; 通过共享记忆、自修复元调试器 (Meta-Debugger) 以及通过 Web 仪表板或 Telegram 进行的人机协同来协同工作。

---

## ARK 流水线

ARK 按顺序运行三个阶段。评审阶段会循环进行，直到论文达到目标分数。

<p align="center">
  <img src="https://idea2paper.org/assets/pipeline_overview.png" alt="ARK Pipeline" width="700">
</p>

| 阶段 | 过程内容 |
|:------|:-------------|
| **研究** | 5步流水线：设置 (conda 环境) &rarr; 分析方案 (researcher) &rarr; 深度研究 (Gemini) &rarr; 专项化 (researcher) &rarr; 引导 (技能与引用) |
| **开发** | 迭代实验循环：计划 &rarr; 在 Slurm 上运行 &rarr; 分析 &rarr; 撰写初稿 |
| **评审** | 编译 &rarr; 评审 &rarr; 计划 &rarr; 执行 &rarr; 验证，循环直到得分 &ge; 阈值 |

### 评审循环

评审阶段的每次迭代包含 **5 个步骤**：

<p align="center">
  <img src="https://idea2paper.org/assets/review_loop.png" alt="Review Loop" width="700">
</p>

| 步骤 | 描述 |
|:-----|:------------|
| **编译** | LaTeX &rarr; PDF，计算页数，生成页面图像 |
| **评审** | AI 评审员评分 (1-10)，列出主要和次要问题 |
| **计划** | 规划器 (Planner) 创建优先处理的任务计划 |
| **执行** | 研究员与实验员并行工作；撰写员修改 LaTeX |
| **验证** | 验证修改是否可编译；重新生成 PDF |

循环将重复进行，直到分数达到录取阈值 &mdash; 或者您通过 Telegram 进行人工干预。

---

## ARK 智能体

<p align="center">
  <img src="https://idea2paper.org/assets/architecture_overview.png" alt="ARK Architecture" width="600">
</p>

| 智能体 | 职责 |
|:------|:-----|
| **研究员** | 分析方案 &rarr; 编写 `idea.md`；基于 Gemini 的文献调研；为项目定制智能体提示词 |
| **评审员** | 根据会议标准为论文评分，生成改进任务 |
| **规划器** | 将评审反馈转化为优先行动计划；分析开发阶段的结果 |
| **撰写员** | 撰写和精炼 LaTeX 章节，并附带经过 DBLP 验证的参考文献 |
| **实验员** | 设计实验，提交 Slurm 任务，分析实验结果 |
| **编程员** | 编写和调试实验代码及分析脚本 |

---

## ARK 的独特之处

| | 其他工具 | ARK |
|---|:------------|:----|
| **控制力** | 完全自主 &mdash; 容易偏离意图，无法中途纠正 | 人机协同：在关键决策点暂停，通过 Telegram 或 Web 引导 |
| **排版** | 布局损坏、LaTeX 错误、需要手动清理 | 硬编码的 LaTeX + 会议模板 (NeurIPS, ACL, IEEE&hellip;) |
| **引用** | LLM 编造看似真实的虚假参考文献 | 每一个引用都经过 DBLP 验证 &mdash; 绝无虚假参考文献 |
| **插图** | 默认样式、尺寸错误、缺乏页面意识 | Nano Banana + 具备页面感知能力的画布、列宽和字体 |
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

ARK 附带 **内置技能** &mdash; 智能体在运行时加载的模块化指令集，用于强制执行最佳实践：

| 技能 | 目的 |
|:------|:--------|
| **研究真实性** | 防模拟提示词：智能体必须运行真实实验，不得编造输出 |
| **人工干预** | 升级协议：智能体在执行不可逆操作前会通过 Telegram 询问 |
| **环境隔离** | 强制执行每个项目的环境边界 |
| **插图真实性** | 验证插图内容与数据匹配；防止占位符或幻觉图表 |
| **页面调整** | 通过调整内容密度而非删除章节来维持页面限制 |

技能存储在 `skills/builtin/` 中，并在流水线引导期间自动安装。

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
| `ark webapp install` | 安装 Web 仪表板服务 |
| `ark access list` | 显示仪表板 Cloudflare Access 允许列表 |
| `ark access add <email>` | 将电子邮件添加到 CF Access 允许列表 |
| `ark access remove <email>` | 从 CF Access 允许列表中删除电子邮件 |
| `ark access add-domain <domain>` | 向 CF Access 添加电子邮件域规则 |
| `ark access remove-domain <domain>` | 从 CF Access 中删除电子邮件域规则 |

---

## 仪表板 (Dashboard)

ARK 包含一个基于 Web 的仪表板，用于管理项目、查看分数和引导智能体。仪表板显示 **实时阶段状态** (Research / Dev / Review)、项目环境状态以及实时成本跟踪。它由单个 FastAPI 进程提供服务 &mdash; 一个端口，一个 systemd 单元。

### 配置

仪表板通过 ARK 配置目录中的 `webapp.env` 进行配置 (默认：项目根目录下的 `.ark/webapp.env`)。该文件在首次运行 `ark webapp` 时自动创建。

#### 身份验证与访问
- **SMTP**: "魔术链接"登录所需。设置 `SMTP_HOST`、`SMTP_USER` 和 `SMTP_PASSWORD`。
- **限制**: 使用 `ALLOWED_EMAILS` (特定用户) 或 `EMAIL_DOMAINS` (整个组织) 来限制访问。
- **Google OAuth**: 可选。设置 `GOOGLE_CLIENT_ID` 和 `GOOGLE_CLIENT_SECRET`。

### 管理命令

| 命令 | 描述 |
|:--------|:------------|
| `ark webapp` | 在前台启动仪表板 (对调试很有用)。 |
| `ark webapp release` | 标记当前代码并部署到生产工作树。 |
| `ark webapp install [--dev]` | 作为 `systemd` 用户服务安装并启动。 |
| `ark webapp status` | 显示 systemd 服务的状态。 |
| `ark webapp restart` | 重启仪表板服务。 |
| `ark webapp logs [-f]` | 查看或跟踪服务日志。 |

<details>
<summary><strong>服务详情 (生产 vs 开发)</strong></summary>

| | 生产 (Prod) | 开发 (Dev) |
|---|:-----|:----|
| **端口** | 9527 | 1027 |
| **服务名称** | `ark-webapp` | `ark-webapp-dev` |
| **Conda 环境** | `ark-prod` | `ark-dev` |
| **代码源** | `~/.ark/prod/` (已固定) | 当前存储库 (实时) |

</details>

<details>
<summary><strong>直接调用编排器</strong></summary>

```bash
python -m ark.orchestrator --project mma --mode paper --max-iterations 20
python -m ark.orchestrator --project mma --mode dev
```

</details>

---

## Docker 使用

### 架构要求

> [!IMPORTANT]
> ARK 研究运行时依赖的科学库在 x86_64 上最稳定。如果您在 **Apple Silicon (M1/M2/M3)** Mac 上构建，必须为 `linux/amd64` 平台构建。
>
> 所有的 ARK Dockerfile 和 `docker-compose.yml` 默认都配置为强制使用 `linux/amd64`。

### 使用 Docker Compose 运行

运行 ARK Web 门户最简单的方法是使用 `docker-compose`。在项目根目录下：

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

ARK 包含一个构建并推送镜像到 Google Artifact Registry 或 GCR 的脚本。

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

您可以使用 ARK 作业容器与 Web 应用程序一起运行隔离的研究作业。取消注释 `docker/docker-compose.yml` 中的 `job` 服务，然后运行：

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

ARK 支持在远程云端虚拟机 (AWS, GCP, Azure) 上运行实验，同时保持编排器和 Web 门户在**本地**运行。这是在不管理 HPC 集群的情况下获得弹性计算能力的推荐设置。

**工作原理：**
1. Web 应用程序在本地或小型服务器上运行，处理项目管理和 UI。
2. 提交项目时，ARK 预配置云端虚拟机，通过 SSH 传输项目代码，并远程管理整个实验生命周期。
3. 结果会自动同步回来。虚拟机在运行完成后终止。

### 通过仪表板启用云端计算

1. 打开**设置**面板 (顶部导航栏中的 ⚙️ 图标)。
2. 滚动到**云端计算**部分。
3. 输入您首选提供商 (AWS, GCP 或 Azure) 的凭据。
4. 点击**保存**。所有后续的项目提交将自动分派到云端。

> [!TIP]
> 云端凭据使用您的 `SECRET_KEY` 在静态时加密。您的密钥绝不会被记录或传输给第三方。

### 配置层级

ARK 为云端计算使用三层配置模型：
1. **系统默认值**: 在 `webapp.env` 中设置 (例如 `CLOUD_REGION`、`CLOUD_NETWORK`)。
2. **全局用户默认值**: 在**设置**面板 (⚙️) 中设置。这些适用于您的所有项目。
3. **项目覆盖**: 在项目创建或重启期间设置。这些具有最高优先级。

这种层级结构允许您只需定义一次标准机器类型和 VPC 设置，同时可以轻松地为特定的高强度实验切换到强大的 GPU 实例。

---

### 创建项目

配置云端计算后，建议通过仪表板启动项目：

1. 从仪表板主页点击**新建项目**。
2. 填写研究目标、目标会议和任何附加说明。
3. 点击**提交** &mdash; Web 应用程序会自动为项目生成 `config.yaml` 并预配置云端虚拟机。

生成的 `config.yaml` 存储在：

```
~/.ark/data/projects/<user_id>/<project_id>/config.yaml
```

您可以随时检查或手动编辑此文件 (例如，调整实例类型或添加 `setup_commands`)。更改在下次运行或重启时生效。

---

### 云端提供商设置

<details>
<summary><strong>☁️ Google Cloud Platform (GCP)</strong></summary>

#### 1. 创建服务账号

```bash
export PROJECT_ID=your-gcp-project-id

# 为 ARK 创建服务账号
gcloud iam service-accounts create ark-runner \
  --display-name="ARK Research Runner"

# 授予所需角色 (Compute Admin + Service Account User)
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:ark-runner@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/compute.admin"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:ark-runner@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountUser"

# 下载 JSON 密钥
gcloud iam service-accounts keys create ~/ark-gcp-key.json \
  --iam-account=ark-runner@${PROJECT_ID}.iam.gserviceaccount.com
```

#### 2. 启用所需的 API

```bash
gcloud services enable compute.googleapis.com --project=$PROJECT_ID
```

#### 3. 在仪表板中配置

将 `~/ark-gcp-key.json` 的内容粘贴到 **GCP Service Account JSON** 字段中，并在设置面板中设置您的 **GCP Project ID**。

#### 4. 查找 GCP 参数 (可选)

如果您需要查找可用区域、机器类型或网络详情，请使用这些 `gcloud` 命令：

```bash
# 列出可用区域
gcloud compute zones list

# 列出特定区域中的可用机器类型
gcloud compute machine-types list --zones=us-central1-a

# 列出网络和子网
gcloud compute networks list
gcloud compute networks subnets list --regions=us-central1

# 列出深度学习镜像 (系列)
gcloud compute images list --project=deeplearning-platform-release --no-standard-images
```

或者，您可以在 **Google Cloud 控制台**中找到这些信息：
- **区域/机器类型**: Compute Engine &rarr; VM 实例 &rarr; 创建实例 (查看选项)
- **网络**: VPC 网络 &rarr; VPC 网络
- **镜像**: Compute Engine &rarr; 镜像

#### 5. `config.yaml` 参考 (高级 / 仅限 CLI)

Web 应用程序会根据您的设置自动生成。对于手动或 CLI 驱动的项目，将以下内容添加到项目的 `config.yaml` 中：

```yaml
compute_backend:
  type: cloud
  provider: gcp
  region: us-central1-a             # GCP 区域 (zone)
  instance_type: n1-standard-8
  image_id: common-cpu              # 深度学习虚拟机镜像系列
  image_project: deeplearning-platform-release
  ssh_key_path: ~/.ssh/id_rsa
  ssh_user: ubuntu
  # 可选：网络
  network: my-vpc                   # 默认: "default"
  subnet: my-subnet                 # 默认: "default"
  # 可选：GPU 加速器
  accelerator_type: nvidia-tesla-t4
  accelerator_count: 1
  # 可选：启动后在实例上运行这些命令
  setup_commands:
    - conda activate base && pip install -r requirements.txt
```

</details>

---

<details>
<summary><strong>☁️ Amazon Web Services (AWS)</strong></summary>

#### 1. 创建 IAM 用户

```bash
# 为 ARK 创建 IAM 用户
aws iam create-user --user-name ark-runner

# 附加策略 (EC2 full access 即可)
aws iam attach-user-policy \
  --user-name ark-runner \
  --policy-arn arn:aws:iam::aws:policy/AmazonEC2FullAccess

# 创建访问密钥
aws iam create-access-key --user-name ark-runner
# 注意输出中的 AccessKeyId 和 SecretAccessKey
```

#### 2. 创建 SSH 密钥对

```bash
# 创建密钥对并保存在本地
aws ec2 create-key-pair \
  --key-name ark-key \
  --query 'KeyMaterial' \
  --output text > ~/.ssh/ark-key.pem
chmod 600 ~/.ssh/ark-key.pem
```

#### 3. 在仪表板中配置

在设置面板中输入您的 **AWS Access Key ID**、**AWS Secret Access Key** 和 **AWS Region** (例如 `us-east-1`)。

#### 4. `config.yaml` 参考 (高级 / 仅限 CLI)

Web 应用程序会根据您的设置自动生成。对于手动或 CLI 驱动的项目，将以下内容添加到项目的 `config.yaml` 中：

```yaml
compute_backend:
  type: cloud
  provider: aws
  region: us-east-1
  instance_type: g4dn.xlarge        # 1x T4 GPU, 4 vCPUs, 16 GB RAM
  image_id: ami-0c7c51e8edb7b66d3   # 深度学习 AMI (Ubuntu 22.04)
  ssh_key_name: ark-key              # AWS 控制台中的密钥对名称
  ssh_key_path: ~/.ssh/ark-key.pem
  ssh_user: ubuntu
  security_group: sg-xxxxxxxx        # 必须允许入站 SSH (端口 22)
  # 可选：启动后设置
  setup_commands:
    - conda activate pytorch && pip install -r requirements.txt
```

> [!IMPORTANT]
> 确保您的安全组允许来自运行 Web 应用程序的机器 IP 的 **入站 SSH (端口 22)**。否则，ARK 无法连接到预配置的实例。

</details>

---

<details>
<summary><strong>☁️ Microsoft Azure</strong></summary>

#### 1. 创建服务主体

```bash
# 登录
az login

# 创建具有 Contributor 角色的服务主体
az ad sp create-for-rbac \
  --name "ark-runner" \
  --role Contributor \
  --scopes /subscriptions/YOUR_SUBSCRIPTION_ID
# 注意: appId (Client ID), password (Client Secret), and tenant (Tenant ID)
```

#### 2. 注册 SSH 公钥

```bash
# 如果您没有 SSH 密钥，请生成一个
ssh-keygen -t rsa -b 4096 -f ~/.ssh/ark-azure-key

# 将自动使用公钥 (~/.ssh/ark-azure-key.pub)
```

#### 3. 在仪表板中配置

在设置面板中输入您的 **Azure Client ID**、**Azure Client Secret**、**Azure Tenant ID** 和 **Azure Subscription ID**。

#### 4. `config.yaml` 参考 (高级 / 仅限 CLI)

Web 应用程序会根据您的设置自动生成。对于手动或 CLI 驱动的项目，将以下内容添加到项目的 `config.yaml` 中：

```yaml
compute_backend:
  type: cloud
  provider: azure
  region: eastus                     # Azure 位置
  instance_type: Standard_NC6s_v3    # 1x V100 GPU, 6 vCPUs, 112 GB RAM
  image_id: UbuntuLTS                # 操作系统镜像别名
  ssh_key_path: ~/.ssh/ark-azure-key
  ssh_user: azureuser
  resource_group: ark-resources      # 如果不存在则创建
  # 可选：启动后设置
  setup_commands:
    - pip install -r requirements.txt
```

</details>

---

### 成本控制

> [!WARNING]
> 云端虚拟机按小时计费。ARK 在每次运行完成后自动终止实例。但是，如果 Web 应用程序进程被意外杀死，**孤儿救援 (Orphan Rescue)** 机制将在下次启动时检测到陈旧实例并将其标记为失败 &mdash; 但**不会自动终止云端虚拟机**。在意外关闭后，请务必在云端控制台中确认没有流浪实例在运行。

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

## 测试 (Testing)

ARK 使用双层测试套件以确保逻辑正确性和真实集成。

### 1. 单元测试 (快速，离线)
覆盖核心逻辑、智能体、记忆和工具，无需真实的 API 访问或云资源。

```bash
# 运行所有单元测试
pytest tests/unit/
```

### 2. 集成测试 (慢速，在线)
验证与外部 API (Claude, Gemini, CrossRef) 和云提供商 (GCP) 的通信。这些测试被标记以防止意外执行和产生费用。

```bash
# 运行访问真实网络 API 的测试 (引用、智能体命令行)
pytest tests/integration/ -m network

# 运行预配置真实 GCP 资源的测试 (需要 ark-gcp-key.json)
# 如果 gcloud 不在您的 PATH 中，请通过 CLI 或环境提供：
pytest tests/integration/ -m gcp --gcloud-path /path/to/google-cloud-key-root/
# 或: export ARK_GCLOUD_PATH=/path/to/google-cloud-key-root/ && pytest tests/integration/ -m gcp
```

### 测试标记 (Markers)
标记在 `pyproject.toml` 中定义，以实现细粒度过滤：
- `-m unit`: 仅逻辑测试。
- `-m integration`: 流水线和云端测试。
- `-m network`: 访问外部互联网 API。
- `-m gcp`: 预配置真实 Google Cloud 资源。

---

## 支持的会议

NeurIPS &bull; ICML &bull; ICLR &bull; AAAI &bull; ACL &bull; IEEE &bull; ACM SIGPLAN &bull; ACM SIGCONF &bull; LNCS &bull; MLSys &bull; USENIX &mdash; 以及 PLDI, ASPLOS, SOSP, EuroSys, OSDI, NSDI, INFOCOM 等别名。

## 许可证

[Apache 2.0](LICENSE)
