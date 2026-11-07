# 确认后交接、缺口处置与自动推进

阶段验收、交接回执和逐 AC 结果采用 [DELIVERY-1](delivery-lifecycle.md)。本文件的 doctor 仅识别交接结构与恢复动作；启用阶段配置后，还必须读取 workflow，完成 specification/engineering 接收回执。结构缺口为零不是阶段已交付。旧材料复用后再评审，不能为迁移状态而重复确认产品或重写已有代码。

## 进入与职责

在恢复项目、接收确认回答、需求封闭、需求版本变化、用户要求开发或出现“不闭合/未就绪”时读取本协议。项目经理对“从已回答到可交接”负责；不能只登记问题或把缺材料退给用户。

运行 `python -X utf8 <skill>/scripts/pm.py --project-root <root> doctor`。可重复添加 `--requirement REQ-ID` 聚焦当前范围；未选中的 REQ 不因本轮被重新确认。`doctor` 默认只读，`--write-report` 刷新完整项目的 `DEVELOPMENT-GUIDE.md`。`init/apply/index` 自动刷新同一份引导和概览页；生成视图不是另一份状态真相。

## 必须执行的推进循环

1. 读取最新 revision、目标版本、已回答问题、封闭来源、锁及工程包。先查详细草案和已有证据，区分“缺材料”和“缺决定”。用户已给出的需求级封闭来源必须保留，不能因补档而要求重新确认相同范围。
2. 执行 doctor，将每个缺口归入：产品决定、AI 可补的设计/工程事实、工程交接、实现任务、实现验证或产品验收。未实现、专项测试未执行、产品验收未完成属于后续阶段，不是准备工程包或开始已获准开发的前置条件。
3. 先做当前授权范围内 AI 能完成的调查、结构化补档、原子功能/状态/验证计划、代码映射、依赖分析、工程包与索引更新。缺结构化字段不等于缺用户决定；从既有确认材料映射时引用来源，不制造决定、五轮记录或验证结果。
4. 对已确认且缺失/过期工程包的需求，自动在当前聊天加载 project-development，进入工程包模式。尚未确认但已有足够上下文时也可调查并形成 draft/blocked 草稿，不能以缺 ready_for_build 拒绝产包。首次产包不足以证明就绪；必须真实调查、填契约、登记 WP 并完成内容复核。未经用户要求实施，不因自动产包启动产品编码。
5. 工程包写当前需求版本、确认来源、实际代码基线（无 Git 时用文件指纹）、模块边界、接口/数据契约、可执行步骤、依赖、验证计划、风险和恢复方式。当前版本内原位更新；跨版本保留历史、评估差异并建立新版，禁止只改目录或复制旧包当成新版已完成。维护版本索引、开发区索引与 canonical docs 根索引。
6. 用 PM 的 apply 接口登记可执行工作包、来源与证据引用，注明 `kind=implementation | verification | foundation`（调查/治理分别用 discovery/governance）。WP 至少写 requirement_ids、next_action、owner、依赖、完成条件和验证方法；共享 WP 明确其工程用途。取消的 WP 或仅有通用治理 WP 不构成工程交接。
7. 重新读取并检查。优先继续不依赖外部决定的下一项；只有出现真正新增的产品取舍、外部资源不可得或用户指定暂停时才留下等待项。待用户回答期间继续无依赖工作，不改锁定范围、不绕过状态机。

## 每项缺口的执行契约

引导至少说明：REQ/版本、原因、影响阶段、补齐动作、步骤、执行者、目标文档/代码、依赖、完成条件、是否需要用户决定和截止安排。日期未知时明确待协商，不编造承诺。脚本生成的 finding ID 是派生诊断标识，不占用 WP/DQ 编号；需要持久排期时按最新状态登记正式 WP，避免重复建任务。

自动队列按优先级与已知依赖排序；`ready_action_ids` 是当前可直接推进项。门禁推进依赖同 REQ 的产品/工程缺口先解决；产包仍可先做调查草稿。目标需求若按既有确认范围属于未来占位、仅引用或由其他 REQ 承载，应先核对归属并记录对应交接方式，禁止为了消除提示创建未授权的独立实现范围。

禁止以“未闭合”“请补齐后继续”“完成定档”“推进工作包”单独收尾。对 AI 可完成的事项，直接执行并说明结果；对用户决定，先准备清楚的推荐、替代、影响和回答入口，不要求用户理解内部字段。

## 检查与完成含义

命令示例：

```text
python -X utf8 <skill>/scripts/pm.py --project-root <root> check --stage structure --strict
python -X utf8 <skill>/scripts/pm.py --project-root <root> check --stage engineering --requirement REQ-ID --strict
python -X utf8 <skill>/scripts/pm.py --project-root <root> doctor --requirement REQ-ID
python -X utf8 <skill>/scripts/pm.py --project-root <root> index
```

- `structure` 检查全项目数据结构、引用和日期；通过不等于当前产品、工程或验收完成。
- `product` 检查当前未决问题及需求级确认是否待处理；仍须人工/AI 结合来源复核真实确认范围。
- `engineering` 检查定档材料、当前版本工程包及索引、可用 WP、开发门禁状态。`planning` 检查活动工程 WP 是否有具体动作；缺计划日期要协商，不能因此阻止已获准的独立调查。
- 默认 `all` 汇总全部阶段发现；长期项目有待定需求时 strict 失败意味着存在具体缺口，不意味着其他独立就绪 REQ 都不能开发。`--requirement` 只限定交接诊断；全项目结构检查继续执行。
- `verification` 和 `acceptance` 现在通过 workflow 的阶段回执及逐 AC 当前证据检查；缺回执为 not_assessed，相关证据变化为 needs_review，均不算通过。可执行 `check --stage verification/acceptance --strict`；实现者与验收者仍须实际复核内容和行为。
- `can_start_full_build` 仅表示机械前提已满足，仍须核对工程内容、授权范围与未人工核验标记。`ready_for_acceptance` 不因诊断补档自动回退或重新开发；`confirmed_for_extension` 只允许已授权基础工作包。

## 锁定需求与收敛

锁定产品范围不变时，沿用确认记录补齐允许的实现元数据、工程包、WP 与 EVD。当前脚本允许给**既有实现面**增加缺失的 execution_steps、interfaces_or_data、failure_and_rollback、observability、test_mapping；不得更改既有值、增删实现面或增加其他契约字段。操作须有有效来源、reason、change_class=wording_only，先 dry-run。其他受保护材料确实缺失时，先在工程包准备与既有范围一致的补充建议；不能偷偷解锁或把补档当成产品变更授权。

独立门禁复核可将 gate.status 从 confirmed_locked 推进 approved，确认来源、协议、范围文本不变；拟提交记录仍须满足完整门禁。不得从 rejected/失败状态借补档直接批准，也不得由此标记验收完成。

当前范围所有适用视角已审查、已有决定已落盘、没有实质新证据或行为缺口时收敛。不要为继续提问发明新能力；可逆工程细节自主处理。只有新证据或新行为取舍才开新问题。

每轮向用户交代三组结果：已可开发/可继续执行；AI 本轮补齐与后续自动动作；真正待用户决定/外部输入及具体入口。不能把 AI 的未完成材料写成“等待用户”。
