# 治理集成与交接协议

阶段配置、签收与覆盖采用 [阶段交付](stage-delivery.md) 和 PM DELIVERY-1。configure_delivery/record_coverage/review_stage/record_delivery_gap 经同一 PM apply 写入既有状态。旧项目缺回执先复核复用，不重开已回答问题、不假定代码不存在。技术验证通过使用 review_stage 合法推进 ready_for_acceptance，产品版本与确认锁保持不变；这与下文仅用于开发前补齐的 gate-only 例外分开。

## 文档与事实所有权

| 内容 | 权威来源 | 工程包保存 |
|---|---|---|
| 需求身份、版本、范围、用户决定、验收标准 | project-manager 状态或项目指定需求规格 | 引用、版本和来源指纹 |
| 工程设计、执行步骤、接口实现细节、审计 | 对应需求版本的工程包及链接契约 | 正文或唯一文档引用 |
| WP、风险、决定和 EVD（已有 PM 时） | project-manager 的 state.json | 稳定 ID 与证据路径 |
| 项目级阶段、当前进度和验收摘要 | project-to-act 既有五本账本或外部账本 | 索引/引用 |
| 源码、测试、生产者/消费者 schema | 仓库文件和 Git 基线 | 文件/符号和验证映射 |

共享接口已有独立规范时继续维护原规范，工程包引用它；不要在两个需求包中各写一份可相互冲突的契约。不要建立新的开发 state.json。PM 的状态字段、工程包文档就绪度和产品验收结果是不同事实。

## 发现与存储

执行已安装 project-to-act 的发现命令：

    python -X utf8 <project-to-act>/scripts/init_project_management.py --project-root <root> --check

managed 模式读取实际配置指定的账本；external-ledger 模式只更新配置指向的权威账本，不另建五本账本。文档根目录沿用已配置/既有的 canonical docs，默认 .project-to-act/docs；不要因为默认路径不同迁移或复制既有文档。

开发区使用 <canonical-docs>/project-development/packages/REQ-ID/vN/ENGINEERING-PACKAGE.md。维护开发区 INDEX.md、各版本目录 INDEX.md 和 canonical docs 的现有根索引；默认根索引是 README.md。每条索引说明路径、用途、读取时机、需求版本和被替代关系。仅在发生实际新增、重命名或删除时改索引。

没有 PM 时，接受项目已有的批准规格和编号，不虚构 PM 状态。没有 project-to-act 时依主入口的普通 docs 路径工作；只在获得持久治理授权时初始化底座，不为运行本 skill 强行创建 PM 状态。

## 输入快照与过期判断

工程包记录 requirement_id、requirement_version、确认来源、human_verification、readiness、PM revision（若有）、WP IDs、规格路径/指纹以及仓库基线和原有工作区差异。版本目录使用来源的语义版本，不用对话次数或时间戳代替。

每次恢复工作、开始受影响实现及交付前重读相关输入。PM 全局 revision 改变不自动使工程包失效，应比较目标需求版本和相关契约；若目标变了，先评估差异，保留旧包历史，将旧版实现/验证标为需复核，新版按新目录建立或更新。缺少产品规则时可以做隔离探索，但不能发布未批准行为。

工程包模式允许输出标为 draft/blocked 的调查和缺口清单；不能把草稿写成可执行开发基线。开发模式要求确认/AI 确认状态和 ready_for_build 同时成立；confirmed_for_extension 只接受明确授权的基础工作包。探索结果不计作完整功能实现。

PM 确认后自动交接时，工程包调查与缺口补齐沿用已有授权，不需要再次邀请用户选择是否产包。先做仓库调查和可逆工程准备；真正未定的产品取舍由 PM 汇总询问。缺口逐项提供目标位置、步骤、依赖、执行者及完成条件。版本变化后评估已有包的契约差异，更新当前版本工程包及索引；不靠复制旧包制造新版本就绪。

## PM 操作适配

先读取所用 pm.py 的帮助和 records-and-archive.md，再使用 inspect/resume/check 及 apply --changes；不能直接编辑 state.json 或生成视图。成功写入后再同步项目账本短摘要，并运行实际支持的检查命令。skill 路由本身不自动创建另一个聊天或授权跨聊天发消息。

支持 doctor 时，用 `doctor --requirement REQ-ID` 检查缺失/过期工程包、索引、定档记录与非取消的工程 WP；用 `check --stage engineering --requirement REQ-ID --strict` 核对交接。结构、工程、验证、验收分别报告；未执行实现/验收不是工程准备阻塞。WP 明确 kind、next_action、负责人、依赖与完成条件，通用治理 WP 不能代替专属实现任务。验证/验收结果必须来自真实证据。

变更文件使用 expected_revision；revise 使用 expected_version；冲突后重读，不盲重放。WP 状态通过 transition 更新，EVD 或决定等按现有 schema 操作，稳定 ID 由最新状态分配，不硬编码示例 ID。

对于当前 pm.py，锁定需求可 revise code_mapping、implementation_evidence、source_map_status，以及 ownership_contract、development_manual_ref、ownership_matrix_ref 等允许的工程元数据。提供 reason、有效 source_ref、change_class=wording_only，避免就绪度回退和证据失效；此分类不能用于产品语义变更。另一个有限例外是：单独提交 implementation_contract 时，仅允许给既有实现面增加缺失的 execution_steps、interfaces_or_data、failure_and_rollback、observability、test_mapping；所有既有值、其他键与列表成员不变。先 dry-run，再 apply/check，以实际脚本为准。

不要为添加工程包链接去修改锁定需求的 source_refs、readiness、implementation_contract 或 interface_spec。唯一的 readiness 例外是：项目经理依据既有用户确认材料完成独立构建门禁复核，并且 `pm.py` 明确支持锁定需求的 gate-only revise 时，可按项目经理门禁规范更新 `readiness`、五轮记录、`gate` 和 `gate_audit`，仅推进到 `ready_for_build`；使用有效审计来源、`reason` 和 `change_class=wording_only`，保持需求版本及确认来源/协议/范围语句不变。不得用该例外标记 `ready_for_acceptance` 或绕过实现验收。工程包链接应记录在关联 WP/EVD 中；需要改变受保护契约时交项目经理处理。代码映射合并既有条目，不覆盖其他模块映射。

## 证据与交接

证据关联 REQ 版本、WP、验收场景、代码提交/文件内容指纹（工作树脏时 HEAD 不足）、环境、命令、时间、退出状态、期望、实际结果和报告路径。代码或环境改变后评估证据是否仍适用，不把旧绿色测试结果当作新版验证。

交接回传：工程包路径、基线和最终源码标识、代码/测试映射、工作包执行结果、通过/失败/未执行项、审计范围与发现、迁移/恢复限制、未人工核验标记和剩余决定。技术就绪不自动转换产品状态或解锁需求。

若 PM 同步或账本写入失败，保留已完成的工程产物，记录“同步未完成”和可重放的有限增量；重新读取后完成一致性修复，不能报告全部交接成功。
