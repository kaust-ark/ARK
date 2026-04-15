# ARK 架构

## 设计原则

**核心理念**：信任 AI 的判断；代码只负责执行和防护栏。

- **数据库为唯一真相来源** &mdash; 项目配置和状态存储在 SQLite ���；YAML 仅用于智能体运行时状态
- **项目级隔离** &mdash; 每个项目拥有独立的 conda 环境、沙盒 HOME 和 `PYTHONNOUSERSITE=1`
- **Skills 优于硬编码规则** &mdash; 模块化指令集（skills）在运行时加载以强制执行最佳实践

## Pipeline 概览

ARK 按三个阶段依次执行：

```
┌─────────────────────────────────────────────────────────────────┐
│                        ARK Pipeline                             │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  阶段 1：Research（4 步）                                        │
│  ┌──────────────┐  ┌─────────────┐  ┌─────────┐  ┌──────────┐ │
│  │Deep Research  │─▶│ 初始化器     │─▶│ 规划器   │─▶│ 实验者   │ │
│  │(Gemini)       │  │(引导)        │  │(规划)    │  │(运行)    │ │
│  └──────────────┘  └─────────────┘  └─────────┘  └──────────┘ │
│                                                                 │
│  阶段 2：Dev                                                     │
│  ┌───────────────────────────────────────────────────────┐     │
│  │  规划 → Slurm 实验 → 分析 → 撰写初稿                    │     │
│  └───────────────────────────────────────────────────────┘     │
│                                                                 │
│  阶段 3：Review（迭代循环）                                       │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐      │
│  │ 编译     │─▶│ 审稿     │─▶│ 规划器   │─▶│ 执行     │──┐   │
│  │ LaTeX    │  │ 评分     │  │ 决策     │  │ 运行     │  │   │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘  │   │
│       ▲                                                   │   │
│       └──── 验证 ◀────────────────────────────────────────┘   │
���             (重新编译)                                         │
│                                                                 │
│  循环直到分数 ≥ 阈值或人工干预                                    │
└─────────────────────────────────────────────────────────────────┘
```

### Research 阶段（4 步流水线）

| 步骤 | 智能体 | 执行内容 |
|:-----|:-------|:---------|
| 1 | Deep Research | Gemini 文献调研，背景知识收集 |
| 2 | 初始化器 | 引导 conda 环境、安装 builtin skills、准备引用 |
| 3 | 规划器 | 根据调研结果生成初始研究计划 |
| 4 | 实验者 | 根据计划运行第一轮实验 |

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

基于 Mixin 的设计，包含 5 个 Mixin：

```python
class Orchestrator(ResearchMixin, DevMixin, ReviewMixin, FigureMixin, BaseMixin):
    # 根据模式分派到正确阶段
    # 每步后同步状态到数据库
    # 处理 Telegram 通知
```

### 4. Skills 系统 (`skills/`)

运行时加载的模块化指令集：

| Skill | 用途 |
|:------|:-----|
| **research-integrity** | 反模拟：智能体必须运行真实实验 |
| **human-intervention** | 通过 Telegram 的升级协议 |
| **env-isolation** | 项目级环境边界 |
| **figure-integrity** | 验证图表与实际数据匹配 |
| **page-adjustment** | 页面限制内的内容密度控制 |

Skills 在流水线引导阶段（Research 阶段第 2 步）自动安装。

### 5. 环境隔离 (`webapp/jobs.py`)

每个项目获得沙盒化的 conda 环境：

- `provision_project_env()` 将基础环境克隆到 `<project>/.env/`
- `project_env_ready()` 检查环境是否存在
- Orchestrator 以 `HOME=<project_dir>`, `PYTHONNOUSERSITE=1` 运行
- CLI (`ark run`) 和 Web 门户均自动检测并使用项目环境

### 6. 状态管理 (`webapp/db.py`)

SQLite 是项目配置和状态的唯一真相来源：

- 项目创建、配置、阶段状态
- 分数历史、成本追踪
- CLI 和 webapp 读写同一个数据库
- `auto_research/state/` 下的 YAML 文件仅用于智能体运行时状态

## 智能体列表（9 个）

| 智���体 | 职责 |
|:-------|:-----|
| 初始化器 | 项目引导：conda 环境、skills、引用 |
| 审稿人 | 评审和评分论文 |
| 规划器 | 分析问题，生成行动计划（论文和开发模式） |
| 实验者 | 设计、运行和分析实验 |
| 研究员 | 文献检索和实验结果分析 |
| 写作者 | 撰写/修改论文章节 |
| 可视化器 | 检查和修复图表质量 |
| 元调试器 | 系统级诊断 |
| 编码器 | 实现代码更改（开发模式） |

## 文件结构

```
ARK/
├── ark/
│   ├── orchestrator.py      # 主循环（基于 Mixin）
│   ├── pipeline.py          # Research 阶段 4 步流水线
│   ├── memory.py            # 分数追踪、问题去重、停滞检测
│   ├── agents.py            # 智能体调用
│   ├── execution.py         # 智能体执行和 skill 注入
│   ├── cli.py               # CLI 命令 (ark new/run/status/...)
│   ├── compiler.py          # LaTeX 编译
│   ├── citation.py          # DBLP/CrossRef 引用验证
│   ├── deep_research.py     # Gemini Deep Research 集成
│   ├── telegram.py          # Telegram 通知 + 人工干预
│   ├── compute.py           # Slurm/云计算后端
│   ├── templates/agents/    # 智能体提示模板
│   │   ├── initializer.prompt
│   │   ├── reviewer.prompt
│   │   ├── planner.prompt
│   │   ├── experimenter.prompt
│   │   ├── researcher.prompt
│   │   ├── writer.prompt
│   │   ├── visualizer.prompt
│   │   └── coder.prompt
│   └── webapp/
│       ├── app.py           # Flask 应用
│       ├── db.py            # SQLite 模型 + 状态管理
│       ├── jobs.py          # 任务启动、conda 环境配置
│       ├── routes.py        # API 路由 + SSE
│       └── static/app.html  # SPA 前端
├── skills/
│   ├── index.json           # Skill 注册表
│   └── builtin/             # 内置 skills
│       ├── research-integrity/
│       ├── human-intervention/
│       ├── env-isolation/
│       ├── figure-integrity/
│       └── page-adjustment/
├── venue_templates/         # 每会议 LaTeX 模板
├── tests/                   # 115 个测试
└── projects/                # 项目目录（gitignored）
```

## 已弃用

- `events.py` — 事件驱动系统（已被规划器决策替代）
- 复杂的记忆追踪（issues, effective_actions, failed_attempts）— 已简化
