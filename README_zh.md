<p align="center">
  <a href="README.md">English</a> &bull; <strong>中文</strong> &bull; <a href="README_ar.md">العربية</a>
</p>

<p align="center">
  <img src="https://kaust-ark.github.io/assets/logo_ark.png" alt="ARK" width="280">
</p>

<h1 align="center">ARK：智能体研究工具包</h1>

<p align="center">
  <strong>从研究想法到终稿论文 — 全自动完成</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.9+-blue.svg" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/license-Apache%202.0-green.svg" alt="Apache 2.0">
  <a href="https://github.com/kaust-ark/ARK/actions/workflows/ci.yml"><img src="https://github.com/kaust-ark/ARK/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/agents-8-orange.svg" alt="8 agents">
  <img src="https://img.shields.io/badge/venues-11+-purple.svg" alt="11+ venues">
</p>

<p align="center">
  <a href="https://kaust-ark.github.io/ARK/"><strong>网站</strong></a> &bull;
  <a href="#快速开始">快速开始</a> &bull;
  <a href="#工作原理">工作原理</a> &bull;
  <a href="#命令行参考">命令行</a> &bull;
  <a href="https://kaust-ark.github.io/architecture.md">架构</a> &bull;
  <a href="https://kaust-ark.github.io/configuration.md">配置</a>
</p>

---

ARK 协调 8 个专业 AI 智能体来**规划实验、编写代码、运行基准测试、撰写 LaTeX 论文，并通过自动化同行评审进行迭代修改** — 直到论文达到出版质量。

只需提供一个研究想法和目标会议，ARK 会处理其余一切。

## ARK 撰写的论文

<p align="center">
<img src="https://kaust-ark.github.io/assets/paper-example.png" alt="MMA 论文" width="480">
<br>
<a href="https://github.com/JihaoXin/mma"><em>CPU 矩阵乘法：从朴素到高效</em></a>
<br>
<sub>NeurIPS 格式 &bull; 6 页 &bull; 14 次迭代</sub>
</p>

## 核心特性

| | 特性 | 详情 |
|---|------|------|
| **8 个智能体** | 审稿人、规划器、实验者、写作者、研究员、可视化器、元调试器、编码器 | 每个项目可自定义提示词 |
| **3 个阶段** | Research &rarr; Dev &rarr; Review | 文献综述、实验开发、迭代改进论文 |
| **Claude Code** | 基于 [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | 建议 Max 订阅 — 极度消耗 token |
| **11+ 会议模板** | NeurIPS、ICML、ICLR、AAAI、ACL、IEEE、ACM、LNCS... | 自动配置页面几何和图表尺寸 |
| **Telegram 机器人** | 实时监控与干预 | 关键决策点主动确认 |
| **计算后端** | Slurm &bull; Local &bull; AWS &bull; GCP &bull; Azure | 在任何平台运行实验 |
| **深度调研** | Gemini Deep Research 集成 | 写作前自动进行文献综述 |
| **Nano Banana** | AI 图表生成 | 通过 Gemini 图像模型生成概念图 |
| **智能恢复** | 断点续传 &bull; 元调试 &bull; 自修复 | 处理 LaTeX 错误、实验失败 |
| **成本追踪** | 每次迭代和累计报告 | 精确了解每次迭代的开销 |

## 工作原理

ARK 按三个阶段依次执行：

<p align="center">
  <img src="https://kaust-ark.github.io/assets/phases_overview.png" alt="ARK 三阶段" width="800">
</p>

| 阶段 | 执行内容 |
|------|---------|
| **研究** | Gemini Deep Research 收集背景知识和文献综述 |
| **开发** | 迭代实验循环：规划实验 → 运行 → 分析结果 → 评估完整性 → 撰写初稿 |
| **审阅** | 迭代改进论文，直到审稿分数达到接受阈值 |

### 审阅阶段步骤

每次审阅迭代经过 4 个步骤：

<p align="center">
  <img src="https://kaust-ark.github.io/assets/review_phase_steps.png" alt="审阅阶段步骤" width="700">
</p>

| 步骤 | 执行内容 |
|------|---------|
| **1. 编译** | LaTeX → PDF，统计页数，提取页面图像 |
| **2. 审阅** | AI 审稿人评分（1–10），识别主要和次要问题 |
| **3. 规划与执行** | 规划器制定行动计划；研究员和实验者并行运行；写作者修改 LaTeX |
| **4. 可视化** | 检查图表尺寸是否符合会议规范，自动修复，重新编译 |

循环持续进行，直到分数达到接受阈值 — 或者你通过 Telegram 进行干预。

### 架构

<p align="center">
  <img src="https://kaust-ark.github.io/assets/architecture.png" alt="ARK 架构" width="700">
</p>

<p align="center">
  <a href="https://kaust-ark.github.io/architecture.md">完整架构文档 &rarr;</a>
</p>

## 快速开始

首先，使用你偏好的工具（conda、uv、venv 等）创建并激活一个 Python 虚拟环境。

```bash
# 1. 安装
pip install -e .

# 2. 创建项目（交互式向导）
ark new mma                    # 例：一篇关于 CPU 矩阵乘法的论文

# 3. 运行 — ARK 接管一切
ark run mma                    # 启动 Research → Dev → Review 循环

# 4. 实时观察
ark monitor mma                # 实时仪表板：智能体活动、分数趋势

# 5. 查看进度
ark status mma                 # 分数: 7.2/10, 迭代: 5, 阶段: Review
```

向导将引导你完成：代码目录、目标会议、研究想法、作者、计算后端、图表生成和 Telegram 设置。

### 从现有 PDF 开始

```bash
# 从提案/草稿中提取标题、作者和研究计划
ark new mma --from-pdf proposal.pdf
```

ARK 使用 PyMuPDF + Claude Haiku 解析 PDF，预填向导内容，可从提取的规格启动完整的论文或开发项目。

## 命令行参考

| 命令 | 功能 |
|------|------|
| `ark new <name>` | 通过交互式向导创建项目 |
| `ark run <name>` | 启动自主循环 |
| `ark status [name]` | 显示分数、迭代、阶段、成本（或列出所有项目） |
| `ark monitor <name>` | 实时仪表板：智能体活动、分数趋势 |
| `ark update <name>` | 注入运行中指令 |
| `ark stop <name>` | 优雅停止 |
| `ark restart <name>` | 停止并重启 |
| `ark research <name>` | 单独运行 Gemini Deep Research |
| `ark config <name> [key] [val]` | 查看或编辑配置 |
| `ark clear <name>` | 重置状态，重新开始 |
| `ark delete <name>` | 完全删除项目 |
| `ark setup-bot` | 配置 Telegram 机器人（一次性） |
| `ark list` | 列出所有项目及状态 |
| `ark webapp install` | 安装生产环境 Web 门户服务（端口 8423） |
| `ark webapp install --dev` | 安装开发环境 Web 门户服务（端口 8424） |
| `ark webapp uninstall` | 停止并移除生产环境服务 |
| `ark webapp release` | 打 tag 并部署当前代码到生产环境 |

> **注意：** `ark webapp --daemon` 已废弃，将在未来版本中移除。请使用 `ark webapp install` 代替。

<details>
<summary><strong>Web 门户（开发/生产环境）</strong></summary>

ARK 提供两个独立数据库的 Web 环境：

| | 生产环境 | 开发环境 |
|--|---------|---------|
| URL | `http://mcmgt01:8423` | `http://mcmgt01:8424` |
| 服务名 | `ark-webapp` | `ark-webapp-dev` |
| 数据库 | `ark_webapp/webapp.db` | `ark_webapp/webapp-dev.db` |
| 代码 | `~/.ark/prod/`（锁定 git tag） | 当前 repo（实时生效） |

```bash
# 首次部署
ark webapp release              # 从当前代码创建生产环境（自动 tag v0.1.0）
ark webapp install              # 启动生产服务（端口 8423）
ark webapp install --dev        # 启动开发服务（端口 8424）

# 发布新版本到生产环境
ark webapp release              # 打 tag、更新生产 worktree、重启服务

# 自定义版本号
ark webapp release --tag v1.0.0
```

开发环境代码修改立即生效，生产环境仅在 `ark webapp release` 后更新。

</details>

<details>
<summary><strong>直接调用 orchestrator</strong></summary>

```bash
# 论文模式，最多 20 次迭代
python -m ark.orchestrator --project mma --mode paper --max-iterations 20

# 开发模式（软件开发，非论文写作）
python -m ark.orchestrator --project mma --mode dev

# 后台运行
nohup python -m ark.orchestrator --project mma --mode paper \
  > auto_research/logs/orchestrator.log 2>&1 &
```

</details>

## Telegram 集成

### 设置步骤

1. 打开 Telegram，向 [@BotFather](https://t.me/BotFather) 发送 `/newbot`，按提示获取 **Bot Token**
2. 运行设置向导：
   ```bash
   ark setup-bot
   ```
3. 粘贴 Bot Token
4. 在 Telegram 中向你的新机器人发送任意消息，然后按 Enter
5. ARK 自动检测 Chat ID 并发送测试消息

凭据保存在 `~/.ark/telegram.yaml`，所有项目共享。

### 功能

- **实时通知** — 分数变化、阶段转换、错误报告
- **发送指令** — 发送消息引导当前迭代方向
- **请求 PDF** — 获取最新编译的论文
- **主动确认** — ARK 在启动 Deep Research 或需要 LaTeX 模板 URL 时会主动询问
- **持久守护进程** — 即使 orchestrator 停止也能继续响应

## 环境要求

- **Python 3.9+** 及 `pyyaml`、`PyMuPDF`
- [**Claude Code**](https://docs.anthropic.com/en/docs/claude-code) CLI 已安装并登录
- **强烈建议 Claude Max 订阅** — ARK 极度消耗 token（每次迭代调用多个智能体）
- 可选：LaTeX（`pdflatex` + `bibtex`）、Slurm、`google-genai`（AI 图表）

```bash
pip install -e .                    # 核心（已含 PyMuPDF）
pip install -e ".[research]"       # + Gemini Deep Research 和 Nano Banana
```

## 更多文档

- [架构与模块参考](https://kaust-ark.github.io/architecture.md)
- [配置、会议模板与计算后端](https://kaust-ark.github.io/configuration.md)
- [测试（84 项测试）](docs/testing.md)

## 路线图与已知问题

完整列表见 [TODO.md](TODO.md)。要点：

- **领域技能集成** — 集成 [claude-scientific-skills](https://github.com/K-Dense-AI/claude-scientific-skills)（170+ 技能，覆盖生物、化学、物理、地理、金融等），支持非计算机领域研究者
- **后端对齐** — Codex 和 Gemini 后端尚未与 Claude Code 完全对齐
- **云计算验证** — AWS/GCP/Azure 计算后端代码已有，但未经端到端验证
- **边缘与定制环境** — 支持离线 HPC、Jetson、受限网络实验室
- **图表排版质量** — 列宽溢出、字体大小不匹配、子图对齐问题
- **引用真实性** — LLM 生成的参考文献可能是幻觉；需要写后通过 Semantic Scholar / CrossRef 验证
- **集成测试** — 尚无端到端 pipeline 测试

## 许可证

[Apache 2.0](LICENSE)
