# project-manager skill（项目经理）

一个把「资深项目经理的需求工程纪律」做成**代码强制执行**的 Agent Skill：让 AI 作为持续项目经理组织立项、稳定拆解与确认产品需求、评审产品设计、管理里程碑与 DDL、处理变更并推动验收结项，同时维护可追溯的跨会话项目事实。

核心特点：方法论不靠 prompt 自觉，而是由确定性状态机工具（`pm.py`）强制执行——稳定 ID、增量修订、乐观锁、状态转移白名单、引用完整性校验、原子写入。

## 仓库结构

```text
project-manager/            # skill 本体
├─ SKILL.md                 # 入口：核心责任、每轮入口流程、模式路由
├─ references/              # 按需加载的方法论文档
│  ├─ lifecycle-modes.md        # 日常/立项/中期/调整/结项 五种交互模式
│  ├─ requirement-decomposition.md  # 需求拆解八步法 + 增量修订规则
│  ├─ product-review-and-delivery.md # 产品评审、DDL、验收与证据
│  └─ records-and-archive.md    # 写入契约、对话归档、隐私边界
├─ scripts/pm.py            # 确定性状态工具（唯一写入通道）
├─ tests/test_pm.py         # 13 条单元测试
├─ assets/change-example.json   # 变更文件示例
└─ agents/openai.yaml       # Codex agent 配置

pm-sandbox/                 # 10 轮角色扮演实测记录（含测试报告与原始对话）
```

## 安装与依赖

- 依赖 `project-to-act` skill 提供账本底座（`.project-to-act/PROJECT_CONFIG.json`），需先于本 skill 初始化。
- 将本仓库克隆到 Agent 的 skills 目录（如 `~/.agents/skills/`），使 `project-manager/SKILL.md` 可被发现。
- Python ≥ 3.10；Windows 需 `pip install tzdata`。

## 快速使用

```bash
# 查看项目状态（未初始化会提示）
python project-manager/scripts/pm.py --project-root <项目根> inspect

# 初始化（要求 project-to-act 已配置）
python project-manager/scripts/pm.py --project-root <项目根> init --project-id <ID> --name <名称>

# 结构化变更（唯一写入通道，带乐观锁）
python project-manager/scripts/pm.py --project-root <项目根> apply --changes changes.json

# 完整性体检 / 跨会话恢复 / 检索 / 对话归档导入
... check | resume | search | import-session
```

状态落盘于 `<项目根>/.project-to-act/docs/project-manager/state.json`（唯一真相源），自动生成 INDEX / REQUIREMENTS / DELIVERY / DECISIONS 四个人类可读视图。

## 方法论要点

- **三层事实分离**：原始诉求 → 产品需求 → 开发工作，靠稳定 ID 关联，禁止整体重写需求树。
- **就绪 ≠ 确认，任务完成 ≠ 产品验收**：确认需验收条件 + 来源 + 理由；完成声明分四档（已实现且已验证 / 已实现未验证 / 模拟或受限成立 / 尚未实现）。
- **预测与承诺分离**：预测延期只更新预测字段，硬截止与用户确认目标不可覆盖，变更用事件连接新旧日期。
- **已确认需求变更治理**：必须带 reason + source_ref + change_class，关联证据自动转待复核、工作包自动标记冲击复审。

## 测试状态

- 单元测试 13/13 通过（`python -m unittest discover -s tests`）。
- 10 轮角色扮演端到端实测通过，覆盖立项、DDL 协商、确认包、变更治理、范围蔓延拦截、中期检查、延期、日常问答零写入、验收四档声明等 16 项纪律验证；实测报告见 [pm-sandbox/测试报告-AI助手10轮.md](pm-sandbox/测试报告-AI助手10轮.md)，完整对话见 [pm-sandbox/原始对话-10轮.md](pm-sandbox/原始对话-10轮.md)。
