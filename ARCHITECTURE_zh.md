# ARK 架构

## 设计原则

**核心理念**：信任 AI 的判断；代码只负责执行和防护栏。

- **数据库为唯一真相来源** &mdash; 项目配置和状态存储在 SQLite 数据库中；YAML 仅用于智能体运行时状态
- **项目级隔离** &mdash; 每个项目拥有独立的 conda 环境、沙盒 HOME 和 `PYTHONNOUSERSITE=1`
- **Skills 优于硬编码规则** &mdash; 模块化指令集（skills）在运行时加载以强制执行最佳实践

## Pipeline 概览

ARK 按三个阶段依次执行：

```
┌─────────────────────────────────────────────────────────────────┐
│                        ARK Pipeline                             │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  阶段 1：Research（5 步）                                        │
│  ┌──────┐  ┌──────────┐  ┌─────────────┐  ┌──────────┐  ┌──────────┐ │
│  │ 配置 │─▶│ 分析提案 │─▶│Deep Research│─▶│ 专项化   │─▶│ 引导启动 │ │
│  │conda │  │ (研究员) │  │  (Gemini)   │  │ (研究员) │  │skills+引用│ │
│  └──────┘  └──────────┘  └─────────────┘  └──────────┘  └──────────┘ │
│                                                                 │
│  阶段 2：Dev                                                     │
│  ┌───────────────────────────────────────────────────────┐     │
│  │  规划 → 实验 (Slurm/云) → 分析 → 撰写初稿              │     │
│  └───────────────────────────────────────────────────────┘     │
│                                                                 │
│  阶段 3：Review（迭代循环）                                       │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐      │
│  │ 编译     │─▶│ 审稿     │─▶│ 规划器   │─▶│ 执行     │──┐   │
│  │ LaTeX    │  │ 评分     │  │ 决策     │  │ 运行     │  │   │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘  │   │
│       ▲                                                   │   │
│       └──── 验证 ◀────────────────────────────────────────┘   │
│             (重新编译)                                         │
│                                                                 │
│  循环直到分数 ≥ 阈值或人工干预                                    │
└─────────────────────────────────────────────────────────────────┘
```

### Research 阶段（5 步流水线）

| 步骤 | 智能体/工具 | 执行内容 |
|:-----|:------------|:---------|
| 0 | — | **配置**：配置项目级 conda 环境（克隆 ark-base —— 仅研究栈，不含 ARK 代码；orchestrator 的 ARK 通过 `PYTHONPATH` 注入） |
| 1 | 研究员 | **分析提案**：读取上传 PDF 或创意 → 写入 `idea.md`（摘要、方法、系统）；输出 Deep Research 查询；解析并提交论文标题 |
| 2 | Gemini | **Deep Research**：文献综述 → `deep_research.md`；通过 Telegram 将 PDF 发送给用户 |
| 3 | 研究员 | **专项化**：生成 `project_context.md`（网页验证）；为项目定制智能体提示模板；选择相关 skills（0–15 个） |
| 4 | — | **引导启动**：安装 builtin skills；引导引用 → `references.bib` |

### Review 循环

每次迭代运行 5 个步骤：编译 → 审稿 → 规划 → 执行 → 验证。

规划器输出结构化 YAML 行动计划：

```yaml
actions:
  - agent: experimenter
    task: "运行困惑度验证实验"
    priority: 1
  - agent: writer
    task: "更新第 4.2 节"
    priority: 2
```

## 核心组件

### 1. 记忆系统 (`memory.py`)

追踪分数、检测停滞、防止重复失败：

```python
class SimpleMemory:
    scores: List[float]       # 分数历史（最近 20 次）
    best_score: float         # 历史最高分
    stagnation_count: int     # 连续停滞计数

    def record_score(score)   # 记录分数
    def is_stagnating()       # 停滞检测
    def get_context()         # 获取上下文（目标锚点 + 分数趋势）
```

附加功能：
- **问题追踪**：基于内容去重 — 统计每个问题在迭代中重复出现的次数
- **修复验证**：验证尝试的修复是否真正解决了问题
- **策略升级**：自动禁用无效方法并建议替代方案
- **元调试**：系统卡住时触发诊断

### 2. 目标锚点

每次智能体调用都包含一个常量"目标锚点"，描述项目的核心目标。这防止智能体在多次迭代后偏离主题。

### 3. 编排器 (`orchestrator.py`)

编排器采用基于 mixin 的设计来组合专业功能：

```python
class Orchestrator(AgentMixin, CompilerMixin, ExecutionMixin, PipelineMixin):
    # AgentMixin: 智能体调用和成本追踪
    # CompilerMixin: LaTeX 编译和 PDF 管理
    # ExecutionMixin: Skill 注入和命令执行
    # PipelineMixin: 高级研究、开发和评审循环
```

- **分派** &mdash; 根据项目的当前模式分派到正确的阶段。
- **同步** &mdash; 在每一步后将状态、分数和进度同步到 SQLite 数据库。
- **处理** &mdash; 处理双向 Telegram 通信和人工干预（HITL）决策。

### 4. Skills 系统 (`skills/`)

运行时加载的模块化指令集，用于指导智能体行为：

| Skill | 用途 |
|:------|:-----|
| **research-integrity** | 反模拟：智能体必须运行真实实验 |
| **human-intervention** | 通过 Telegram 针对阻塞情况的升级协议 |
| **env-isolation** | 每个项目的环境边界和安全性 |
| **figure-integrity** | 验证图表是否符合实际实验数据 |
| **page-adjustment** | 内容密度控制，以符合会议页数限制 |

Skills 在流水线引导（研究阶段第 4 步）期间自动安装。

### 5. 环境隔离 (`website/dashboard/jobs.py`)

每个项目获得沙盒化的 conda 环境：

- `provision_project_env()` 将基础环境克隆到 `<project>/.env/`
- `project_env_ready()` 检查环境是否存在
- 编排器以 `HOME=<project_dir>`, `PYTHONNOUSERSITE=1` 运行
- CLI (`ark run`) 和仪表盘均自动检测并使用项目本地环境。

### 6. 计算后端 (`ark/compute/`)

ARK 支持多种计算后端运行实验：

- **Local**: 直接在宿主机上运行实验。
- **Slurm**: 使用 `sbatch` 将作业提交到 HPC 集群。
- **Cloud**: 在 **AWS**、**GCP** 或 **Azure** 上配置实例。
- **Custom**: 用于特殊环境的可扩展后端。

云后端处理全生命周期：配置、代码传输 (rsync)、设置、执行、结果收集和销毁。

### 7. AI 图表生成 (`ark/nano_banana.py`)

**Nano Banana** 是一个基于 Gemini 的系统，用于生成高质量的科学图表：

- **Planner**: 根据论文上下文设计详细的视觉规范。
- **Stylist**: 改进规范以匹配学术出版物的美学。
- **Visualizer**: 使用 Gemini 图像生成模型生成图像。
- **Critic**: 评估图表并提供反馈以进行迭代改进。

## 智能体列表（6 个）

| 智能体 | 职责 |
|:-------|:-----|
| researcher | 分析提案 → `idea.md`；文献综述；为项目定制智能体提示模板并选择 skills |
| reviewer | 评审和评分论文；检查实验与提案的一致性 |
| planner | 分析问题，生成行动计划（论文和开发模式）；验证实验一致性 |
| writer | 编写/修订论文章节，引用经 DBLP 验证 |
| experimenter | 设计、运行和分析实验；支持 Slurm 和云后端 |
| coder | 实现代码更改（开发模式） |

## 文件结构

```
ARK/
├── ark/
│   ├── orchestrator.py      # 主循环（基于 Mixin）
│   ├── pipeline.py          # 第 1 阶段（研究）和第 2 阶段（开发/评审）逻辑
│   ├── memory.py            # 分数追踪、问题去重、停滞检测
│   ├── execution.py         # 智能体执行和 skill 注入
│   ├── cli.py               # CLI 命令 (ark new/run/status/access/...)
│   ├── compute/             # 计算后端 (Local, Slurm, AWS, GCP, Azure)
│   ├── engines/             # 智能体编排和后端运行时 (Claude, Gemini)
│   ├── orchestrator/        # 状态和工作区管理
│   ├── telegram/            # Telegram 通知 + 双向机器人
│   ├── website/             # 仪表盘和主页 (FastAPI + SQLite)
│   ├── nano_banana.py       # AI 图表生成流水线
│   ├── citation.py          # DBLP/CrossRef 引用验证
│   ├── deep_research.py     # Gemini Deep Research 集成
│   ├── compiler.py          # LaTeX 编译逻辑
│   └── templates/agents/    # 智能体提示模板
├── website/                 # Web 界面
│   ├── dashboard/           # FastAPI 后端 + SQLite 数据库
│   └── homepage/            # 静态落地页
├── skills/                  # 模块化指令集
│   ├── index.json           # Skill 注册表
│   ├── builtin/             # 内置 skills（自动安装）
│   └── library/             # 特定领域 skills（由研究员选择）
├── venue_templates/         # 每会议 LaTeX 模板
├── tests/                   # 全面的测试套件
└── projects/                # 项目目录（gitignored）
```

## 已弃用 / 已删除

- `events.py` — 事件驱动系统（已被规划器决策替代）
- 复杂的记忆追踪（issues, effective_actions, failed_attempts）— 已简化
- `initializer` 智能体 — 已合并到 `researcher`（分析提案步骤）
- `visualizer` 智能体 — 已删除（死代码，流水线中从未调用）
- `meta_debugger` 智能体 — 已删除（只能诊断无法行动；由流水线级停滞检测替代）
- `ark/webapp/` Python 模块 — 已迁移到 `website/dashboard/`
