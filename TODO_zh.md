# ARK 待办事项与已知问题

## 最近完成（v0.2）

### [x] 项目级 conda 环境隔离
- 每个项目拥有独立的 `.env/` conda 环境，从基础环境克隆
- 沙盒 HOME、`PYTHONNOUSERSITE=1`、隔离的 PYTHONPATH
- CLI (`ark run`) 和 Web 门户均自动检测并使用项目环境
- 流水线引导（Research 阶段第 2 步）在缺失时自动配置

### [x] 4 步 Research 阶段流水线
- Deep Research → 初始化器 → 规划器 → 实验者
- 初始化器智能体引导环境、skills 和引用

### [x] 内置 5 个 builtin skills 的 Skills 系统
- research-integrity、human-intervention、env-isolation、figure-integrity、page-adjustment
- 在流水线引导阶段自动安装

### [x] 反模拟 / 反捷径强制执行
- 提示防止智能体伪造实验结果
- 在实验者、规划器和写作者智能体中加固

### [x] 人工干预协议
- 智能体在执行不可逆操作前通过 Telegram 向用户请求确认

### [x] 数据库为唯一真相来源
- SQLite 存储项目配置、状态、分数、成本
- CLI 和 webapp 统一使用同一数据库
- YAML 仅用于智能体运行时状态

### [x] Telegram 富文本通知 + HPC SSL
- 格式化的分数变化、阶段转换、智能体活动消息
- 支持企业/HPC 网络的自签名证书

### [x] Web 门户阶段徽章 + 成本追踪
- 实时 Research / Dev / Review 徽章
- 项目级 conda 环境状态显示
- 实时 token 和成本追踪仪表板

## 集成与生态系统

### [ ] 集成 claude-scientific-skills
- 仓库：https://github.com/K-Dense-AI/claude-scientific-skills
- 170+ 领域 skills（生物信息学、化学、地理空间、金融、量子等）
- 零代码集成：将 skills 复制到 `~/.claude/skills/`，ARK 智能体自动发现
- 策略：不要安装全部 170+，按领域精选以避免 token 膨胀
- 添加领域 skill 推荐部分到 ARK 文档
- 测试：验证 skills 在智能体通过 `claude -p` 配合 `--no-session-persistence` 运行时正确加载

### [ ] Codex 后端 — 完整功能对等
- 基本调用有效（`codex exec`），但未在真实项目上端到端测试
- 缺失：深度研究上下文注入（Codex 无 Gemini Deep Research 等效功能）
- 缺失：计算后端集成验证（Slurm、云）
- 需要测试权限模型（`--dangerously-bypass-approvals-and-sandbox` 的影响）

### [ ] Gemini 后端 — 完整功能对等
- Deep Research 集成有效，但智能体工具可用性与 Claude 不同
- WebSearch/WebFetch 在 Gemini CLI 中的行为可能不同
- 需要验证：Gemini CLI 是否尊重 `~/.claude/skills/`？（可能不会 — skills 是 Claude Code 特有的）
- 可能需要 Gemini 原生的 skill 注入机制

## 云与计算

### [ ] AWS 云计算 — 端到端验证
- 计算后端代码存在（EC2 配置、rsync、SSH 执行）但从未在真实 AWS 上验证
- 需要测试：实例配置、安全组设置、GPU 实例类型、竞价实例 vs 按需
- 需要测试：云计算小时的成本追踪准确性
- 需要测试：实验完成后的清理/终止

### [ ] GCP / Azure 云计算 — 验证
- 与 AWS 相同 — 代码存在，未在生产中测试
- GCP：验证 gcloud CLI 集成、GPU 配额处理
- Azure：验证 az CLI 集成、VM 配置

### [ ] 边缘设备与自定义环境支持
- 当前假设：智能体在拥有完整互联网、pip/conda 和 GPU 访问的机器上运行
- 边缘场景：Jetson、Raspberry Pi、有限连接实验室、气隙 HPC
- 需要：环境能力检测（可用什么？GPU？互联网？包管理器？）
- 需要：工具/包不可用时的优雅降级
- 需要：预构建的 conda 环境规格或 Docker 镜像以确保可复现性
- 考虑：研究人员预下载包和数据的离线模式

## 论文质量

### [ ] 图表视觉布局 — 已知问题
- 图表有时溢出栏宽或标签被裁剪
- 图表中的字号可能与会议模板正文不匹配
- 多面板图表对齐可能偏移（子图间距）
- 可视化器智能体诊断问题但修复有时浮于表面（如仅调整 figsize 而未修复底层布局）
- 需要：更严格的编译后视觉检查 — 将渲染的 PDF 区域与模板规格对比
- 考虑：文本/图表碰撞的像素级重叠检测

### [x] 引用真实性与幻觉
- 实现了 API 优先的引用系统（`ark/citation.py`）
- LLM 从不编写 BibTeX — 所有条目从 DBLP / CrossRef 官方 API 获取
- 搜索级联：DBLP → CrossRef → arXiv → Semantic Scholar
- 研究员智能体仅从 API 验证的候选列表中选择论文
- 每次迭代验证：每个审稿周期重新验证 `references.bib`
- 双源交叉确认（DBLP + CrossRef）
- 预印本 → 已发表版本自动升级
- 未使用引用清理（从 `.bib` 中移除未引用条目）
- CLI 工具：`ark cite-check`、`ark cite-search`、`ark cite-debug`

### [ ] 表格格式
- 表格可能在双栏会议中溢出栏/页宽
- `tabular` vs `tabular*` vs `tabulary` 选择不总是正确
- 需要：可视化器阶段的表格宽度验证

## 智能体健壮性

### [ ] 停滞检测改进
- 元调试器捕获部分停滞模式但遗漏其他
- 已知差距：智能体产出输出但未取得实质进展（冗长但空洞）
- 需要：迭代间论文的语义差异分析 — 如果变化微不足道，则升级

### [ ] 多语言论文支持
- 当前假设英文论文
- 部分会议接受其他语言（如中国 CS 会议）
- 低优先级但值得注意

## 开发者体验

### [ ] 测试覆盖差距
- 存在 115 个测试但主要是单元级
- 没有运行迷你流水线端到端的集成测试
- 需要：一个在 < 5 分钟内运行 规划 → 实验 → 撰写 → 审稿 的小型合成项目

### [ ] 配置验证
- `config.yaml` 错误（拼写、缺少字段）有时在流水线深处导致晦涩失败
- 需要：启动时的前置模式验证和清晰错误消息
