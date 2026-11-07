# 记录与归档

## 目录

```text
.project-to-act/
├─ PROJECT_*.md                         # project-to-act 当前治理视图
├─ docs/project-manager/
│  ├─ state.json                       # 项目经理细化状态唯一真相源
│  ├─ INDEX.md                         # 自动生成入口
│  ├─ REQUIREMENTS.md                  # 自动生成需求视图
│  ├─ DELIVERY.md                      # 自动生成交付视图
│  ├─ DECISIONS.md                     # 自动生成决定与评审视图
│  └─ PROJECT_OVERVIEW.html             # 自动生成的静态项目进度概览页
└─ local/project-manager/
   ├─ conversations/*.jsonl            # 本机对话归档，不入 Git
   └─ search-index.json                # 可重建本机索引
```

`state.json` 保存细化状态，五本账本保存项目级当前结论并引用详细文档。不要手工编辑自动生成视图。既有项目可以映射已有文档，不自动迁移或重编号。

`PROJECT_OVERVIEW.html` 由 `pm.py` 从同一份 `state.json` 生成，展示产品概述、预期功能、功能确认/就绪状态、实现思路、工作包、风险、未决问题、最近状态变化和本轮工作汇报。它是静态文件，不是实时服务；`init`、`apply` 或 `index` 后自动重建。功能列表中状态不是 `可开发/验收` 的条目必须进入当前关注区，便于发现尚未敲定的功能。

概览页和 `resume` 的工作汇报必须包含显眼的下一步行动区。每条活动工作包或需求门禁行动都要带 `action`、`owner`、`due_at` 和 `acceptance_criteria`；行动可从工作包字段读取，缺失时从关联需求补充验收条件，仍缺失的字段显示为“待补齐”。这组字段是项目经理的执行闭环，不得只汇报状态而不说明谁在何时交付什么结果。

同一份汇报还要提供“每日进度汇总”：按项目时区聚合 `change_events` 的状态操作、工作包推进/完成/阻塞和已导入公开 session 的消息量，显示每日工作量与推进项目。每一天先做项目经理式提炼，把相关操作归并为推进主题，用具体产品、设计、实现改动说明“今天到底改了什么”，并说明这些改动让累计进度如何变化；随后给出量化摘要、代表性结论和可展开的原始明细，再按需求/功能、开发工作包、产品决策、证据来源、里程碑、风险评审和 Session 分组列出具体修改，避免按时间流水导致难以观察。长的功能定义、实现契约和验收内容默认折叠，页面优先显示当前行动和每日提炼。

`resume` 返回同一份进度摘要：已完成工作包、进行中与下一步、待敲定需求、开放风险、最近状态变更和已导入 session 归档。session 归档只读取经过导入和脱敏的用户/助手公开消息；没有归档时，以状态变更和结构化决定作为当前工作总结，不编造会话原文。

`DEVELOPMENT-GUIDE.md` 同样从最新状态、工程包文件和索引派生，init/apply/index 自动刷新；doctor 默认只读，doctor --write-report 写完整项目引导。它列出每个交接缺口的原因、目标位置、步骤、执行者、完成条件和是否需用户决定；派生 ID 不替代正式 WP。resume/概览必须保留这些 REQ 级动作，不能因存在通用工作包而省略。工程内容复核、实现验证和产品验收不由文件存在自动推断。配置使用其他文档根时，PROJECT_CONFIG.json 的 canonical_docs_root 可显式指定工程区根；该设置不迁移 PM 状态或既有文档。

需求确认问题属于 `state.json` 当前状态的一部分。`decision_questions` 中状态为 `accepted` 或 `overridden` 的问题视为已回答；历史定档文档、会话归档和搜索索引只提供证据，不能重新打开已回答问题。只有新来源、新证据、冲突或用户可观察行为变化才允许追加问题并升级需求版本。

## 跨会话协作状态

跨 session 只共享轻量状态提示，不另建第二份产品真相。会话开始读取最新 revision、目标 REQ 版本、最近 change event 和 confirmation lock；本地会话状态使用 `working | waiting_user | completed | blocked`，记录目标 REQ、目标版本、起始/最后 revision 和更新时间。`state.json` 的 `expected_revision` 与 `write.lock` 负责防止静默覆盖；发生 revision 冲突时重新读取状态，不能重放旧变更。已锁定或已回答的问题只读复用，除非用户明确提出范围变更。

## 写入契约

所有改变通过 `pm.py apply --changes <file>`。变更文件包含 `expected_revision` 和 `operations`。工具执行乐观版本检查、状态转换检查、引用检查和原子写入，然后重建视图。失败不更新 revision。

操作包括：

阶段配置与交付另有 `configure_delivery`、`record_coverage`、`record_delivery_gap`、`review_stage`，格式与证据规范见 [DELIVERY-1](delivery-lifecycle.md)。这些操作沿用同一 revision/写锁/原子事务，不升级需求版本或改确认锁。阶段配置位于 project.delivery_workflow，结果位于 requirements[].delivery；不能用普通 revise 写入 delivery，也不能手改状态。DELIVERY-WORKFLOW.md 是派生视图，不是新状态源。

- `set_project`：修改项目阶段、当前模式、名称、目标和时区等项目字段。
- `add`：向受支持集合加入带稳定 ID 的记录。
- `revise`：带 `expected_version` 修改条目并自动升级版本。
- `transition`：带预期旧状态改变需求、工作包、里程碑或风险状态。
- `add_source_ref`：给既有条目增加来源，不改变语义版本。

`unlock_confirmation` 只用于有明确来源的范围变更：工具将原确认锁、确认包和门禁材料保存在 `confirmation_history`，新版本回到 `pending_confirmation / ready_for_design`。旧包不被改写为新版本已展示的确认；已回答问题继续保留，重新定档只处理实际变化。
AI 复核版本使用 `ai_confirmed_unverified`、`human_verification=unverified` 和 `gate.confirmation_protocol=ai_review_unverified`；该协议不能用于标记人工 `confirmed`，也不绕过完整工程设计与就绪检查。

需求、工作包、里程碑、风险受状态机约束，状态只能通过 `transition` 改变。评审和证据不设状态机，状态通过 `revise` 修改并随版本升级留痕。

语义修订已确认需求必须提供 `reason`、`source_ref` 和 `change_class`。确认状态的变更必须能定位到用户确认或项目指定的验收机制。

## 对话归档

导入时要求明确 `project-id`、`session-id` 和文件。仅提取用户与助手的公开消息正文；过滤 system、developer、reasoning 和工具输出。每条记录保存来源定位、时间、角色、内容哈希和脱敏状态。

密钥形态、Authorization 头、常见 token 字段和私密键值进行脱敏。归档的是脱敏原文时必须标记，不能声称与源文件逐字节一致。导入按消息指纹去重，可安全重试。

会话关联有歧义时不要全盘扫描；要求用户选择明确会话。原文不可用时只记录摘要和缺失状态。

## Git 和隐私

初始化会确保 `.project-to-act/local/` 被 `.gitignore` 排除。该规则只控制 Git，不代表文件不会被网盘、备份或杀毒软件读取。敏感原始资料仍应放在用户授权的受控位置。

## 故障恢复

结构化状态写临时文件、同步落盘后原子替换。生成视图失败时保留状态并在下一次 `index` 重建。并发写入通过 revision 拒绝静默覆盖。索引可删除重建，不能成为唯一事实源。

## project-to-act 同步

项目经理工具不自动改写五本账本，以避免覆盖项目自定义结构。每次成功应用后，Agent 将简短项目阶段、当前焦点、功能状态、里程碑和验收结论同步到对应账本，并使用详细文档路径作为引用；写入后运行 project-to-act 验证。同步条目以行内形式（列表项或加粗行）追加到账本既有小节，不新增标题层级，避免触发账本校验警告。
