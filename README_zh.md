<p align="center">
  <a href="README.md">English</a> &bull; <strong>中文</strong> &bull; <a href="README_ar.md">العربية</a>
</p>

<p align="center">
  <img src="https://kaust-ark.github.io/assets/logo_ark_transparent.png" alt="ARK" width="260">
</p>

<h1 align="center">ARK &mdash; 智能体研究工具包</h1>

<p align="center">
  <em>自动化苦活，不自动化方向。</em>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.9+-blue.svg" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/license-Apache%202.0-green.svg" alt="Apache 2.0">
  <a href="https://github.com/kaust-ark/ARK/actions/workflows/ci.yml"><img src="https://github.com/kaust-ark/ARK/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/agents-8-orange.svg" alt="8 Agents">
  <img src="https://img.shields.io/badge/venues-11+-purple.svg" alt="11+ Venues">
  <img src="https://img.shields.io/badge/tests-106-brightgreen.svg" alt="106 Tests">
</p>

<p align="center">
  <a href="https://kaust-ark.github.io/"><strong>网站</strong></a> &bull;
  <a href="#快速开始">快速开始</a> &bull;
  <a href="#ark-pipeline">Pipeline</a> &bull;
  <a href="#ark-agents">Agents</a> &bull;
  <a href="#命令行参考">命令行</a>
</p>

---

ARK 协调 **8 个专业 AI 智能体**，将研究想法转化为论文——文献调研、Slurm 实验、LaTeX 撰写、图表生成与迭代审稿——你随时通过 **CLI**、**Web 门户**或 **Telegram** 掌控全局。

```
给它一个想法和目标会议，ARK 处理其余一切。
```

## ARK 撰写的论文

<p align="center">
<img src="https://kaust-ark.github.io/assets/paper-example.png" alt="MMA 论文" width="480">
<br>
<a href="https://github.com/JihaoXin/mma"><em>CPU 矩阵乘法：从朴素到高效</em></a>
<br>
<sub>NeurIPS 格式 &bull; 6 页 &bull; 14 次迭代</sub>
</p>

---

## ARK Pipeline

ARK 按三个阶段依次执行。Review 阶段循环迭代直到论文达到目标分数。

<p align="center">
  <img src="https://kaust-ark.github.io/assets/pipeline_overview.png" alt="ARK Pipeline" width="700">
</p>

| 阶段 | 执行内容 |
|:------|:---------|
| **Research** | Gemini Deep Research 文献调研与背景知识收集 |
| **Dev** | 迭代实验循环：规划 &rarr; Slurm 运行 &rarr; 分析 &rarr; 撰写初稿 |
| **Review** | 编译 &rarr; 审稿 &rarr; 规划 &rarr; 执行 &rarr; 验证，循环直到分数 &ge; 阈值 |

### Review 循环

每次 Review 迭代经过 **5 个步骤**：

<p align="center">
  <img src="https://kaust-ark.github.io/assets/review_loop.png" alt="Review 循环" width="700">
</p>

| 步骤 | 说明 |
|:-----|:-----|
| **Compile** | LaTeX &rarr; PDF，统计页数，提取页面图像 |
| **Review** | AI 审稿人评分 1&ndash;10，列出主要和次要问题 |
| **Plan** | 规划器生成优先级行动计划 |
| **Execute** | 研究员 + 实验者并行运行；写作者修改 LaTeX |
| **Validate** | 验证变更可编译，重新生成 PDF |

循环持续直到分数达到阈值——或你通过 Telegram 干预。

---

## ARK Agents

<p align="center">
  <img src="https://kaust-ark.github.io/assets/architecture_overview.png" alt="ARK 架构" width="600">
</p>

| 智能体 | 职责 |
|:-------|:-----|
| **Reviewer** | 按会议标准评分，生成改进任务 |
| **Planner** | 将审稿意见转化为优先级行动计划 |
| **Writer** | 撰写和打磨 LaTeX 章节，引用经 DBLP 验证 |
| **Experimenter** | 设计实验、提交 Slurm 任务、分析结果 |
| **Researcher** | 通过学术 API（DBLP、CrossRef、Semantic Scholar）深度文献调研 |
| **Visualizer** | 基于 Nano Banana 和会议画布尺寸生成出版级图表 |
| **Meta-Debugger** | 识别流程停滞、诊断故障、触发自修复 |
| **Coder** | 编写和调试实验代码与分析脚本 |

---

## ARK 有何不同

| | 其他工具 | ARK |
|---|:---------|:----|
| **控制** | 全自动运行，偏离意图，无法中途纠偏 | 人机协同：关键决策暂停，Telegram 或网页随时介入 |
| **排版** | 布局混乱、LaTeX 报错、大量人工修复 | 硬编码 LaTeX + 会议模板（NeurIPS、ACL、IEEE……） |
| **引用** | LLM 编造看似合理但不存在的引用 | 每条引用经 DBLP API 验证，杜绝虚假文献 |
| **图表** | 默认样式、尺寸失控、无视页面约束 | Nano Banana + 会议画布尺寸、栏宽、字号精确匹配 |

---

## 快速开始

```bash
# 安装
pip install -e .

# 创建项目（交互式向导）
ark new mma

# 运行——ARK 接管一切
ark run mma

# 实时监控
ark monitor mma

# 查看进度
ark status mma
```

向导将引导你完成：代码目录、目标会议、研究想法、作者、计算后端、图表生成和 Telegram 设置。

### 从现有 PDF 开始

```bash
ark new mma --from-pdf proposal.pdf
```

ARK 使用 PyMuPDF + Claude Haiku 解析 PDF，预填向导，从提取的规格启动项目。

---

## 命令行参考

| 命令 | 功能 |
|:-----|:-----|
| `ark new <name>` | 通过交互式向导创建项目 |
| `ark run <name>` | 启动自主 pipeline |
| `ark status [name]` | 分数、迭代、阶段、成本 |
| `ark monitor <name>` | 实时仪表板：智能体活动、分数趋势 |
| `ark update <name>` | 注入运行中指令 |
| `ark stop <name>` | 优雅停止 |
| `ark restart <name>` | 停止并重启 |
| `ark research <name>` | 单独运行 Gemini Deep Research |
| `ark config <name> [key] [val]` | 查看或编辑配置 |
| `ark clear <name>` | 重置状态，重新开始 |
| `ark delete <name>` | 完全删除项目 |
| `ark setup-bot` | 配置 Telegram 机器人 |
| `ark list` | 列出所有项目及状态 |
| `ark webapp install` | 安装 Web 门户服务 |

<details>
<summary><strong>Web 门户（开发/生产环境）</strong></summary>

| | 生产环境 | 开发环境 |
|--|:---------|:---------|
| 端口 | 8423 | 8424 |
| 服务名 | `ark-webapp` | `ark-webapp-dev` |
| 代码 | `~/.ark/prod/`（锁定 git tag） | 当前 repo（实时生效） |

```bash
ark webapp release              # 打 tag + 部署到生产
ark webapp install              # 启动生产服务（端口 8423）
ark webapp install --dev        # 启动开发服务（端口 8424）
```

</details>

<details>
<summary><strong>直接调用 orchestrator</strong></summary>

```bash
python -m ark.orchestrator --project mma --mode paper --max-iterations 20
python -m ark.orchestrator --project mma --mode dev
```

</details>

---

## Telegram 集成

```bash
ark setup-bot    # 一次性配置：粘贴 BotFather token，自动检测 chat ID
```

功能：
- **实时通知** &mdash; 分数变化、阶段转换、错误报告
- **发送指令** &mdash; 引导当前迭代方向
- **请求 PDF** &mdash; 获取最新编译论文
- **主动确认** &mdash; 关键决策前主动询问

---

## 环境要求

- **Python 3.9+** 及 `pyyaml`、`PyMuPDF`
- [**Claude Code**](https://docs.anthropic.com/en/docs/claude-code) CLI 已安装并登录
- **建议 Claude Max 订阅** &mdash; 极度消耗 token
- 可选：LaTeX（`pdflatex` + `bibtex`）、Slurm、`google-genai`（AI 图表）

```bash
pip install -e .                    # 核心
pip install -e ".[research]"       # + Gemini Deep Research 和 Nano Banana
```

## 支持的会议模板

NeurIPS &bull; ICML &bull; ICLR &bull; AAAI &bull; ACL &bull; IEEE &bull; ACM SIGPLAN &bull; ACM SIGCONF &bull; LNCS &bull; MLSys &bull; USENIX &mdash; 另有 PLDI、ASPLOS、SOSP、EuroSys、OSDI、NSDI、INFOCOM 等别名。

## 许可证

[Apache 2.0](LICENSE)

<p align="center">
  <sub>由 <a href="https://sands.kaust.edu.sa/">KAUST SANDS 实验室</a> 构建</sub>
</p>
