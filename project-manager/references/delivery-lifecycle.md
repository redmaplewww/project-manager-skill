# 阶段验收与开发交接（DELIVERY-1）

适用于需求确认后的工程准备、开发、验证和交付。与功能定档配合使用：产品决定沿用已确认来源，内部材料缺失由 AI 补齐，不能要求用户重复确认。阶段检查是内容评审加可核对证据，不是检查标题是否齐全。

## 五个阶段及责任

| 阶段 ID | 交出 → 接收角色 | 必需工作产品 | 通过的含义 |
|---|---|---|---|
| specification | PM → 工程设计 | 固化规格、决定/非目标、原子功能、状态/异常、接口边界、稳定 AC | 用户决定落盘，范围可执行，验收设计完整 |
| engineering | 工程设计 → 实现 | 工程包、内容清单、逐 AC 追踪、UI/接口/数据设计、WP、测试与回滚计划 | 开发方已完成内容评审并接收；不要求代码已经实现 |
| implementation | 实现 → 技术验证 | 所有必须行为、前后端、配置/迁移/失败恢复、代码映射 | 全部本期 AC 有完整实现；局部切片不能算整个 REQ 完成 |
| verification | 技术验证 → 产品验收 | 逐 AC 当前证据、接口/集成、系统/UI、实际运行、源码审计 | 必须验证通过且源码/测试/报告版本匹配；可进入验收 |
| acceptance | 指定验收者 → PM | 用户场景结果、运行/使用说明、限制、明确验收结论 | 指定版本产品被有权验收者接受；不能用需求确认替代 |

一个 REQ 可以迭代，多个 REQ 可以处于不同阶段。不把全项目未完成当成所有工作都受阻。先完成当前可执行项；只有受影响工作等待新增产品决定、外部输入或用户暂停。

用户确认 → 自动固化规格 → 自动工程调查/产包/评审 → 交接回执。以上都是需求交付的内部工作，不能在仅写入 confirmed 后就宣称“已完成需求交接”。用户仅要求确认时做到工程准备；已有完整开发授权时接续实现、验证和交付，不在产包后等待重复指令。

## 配置与唯一状态

阶段配置在既有 PM state.json 的 project.delivery_workflow，回执、覆盖和缺口在对应 requirements[].delivery。不新建开发状态文件。工程包 JSON 是工程设计内容清单，不能存第二份进度状态。

通过 apply 提交 `configure_delivery`（沿用 expected_revision 并先 dry-run）：

```json
{
  "op": "configure_delivery",
  "source_ref": "SRC-实际授权来源",
  "config": {
    "profile": "full_product",
    "scope": [{"id": "REQ-实际编号", "version": 1, "mode": "full", "ui_required": true}],
    "additional_checks": {"verification": ["项目实际要求的质量检查"]}
  }
}
```

scope 列出本期完整范围及当前版本；不能为消除缺口静默删项。full 只能用于本期完整能力；confirmed_for_extension 仅用 foundation，汇报必须显示基础层范围。基础检查不可配置关闭，项目可增加质量检查。没有 UI 的需求写 ui_required=false，不能为躲过前端开发这样分类。阶段验收项允许说明不适用并附依据，但不适用不能抵消派生的逐 AC 实现/验证约束。

配置升级保留历史；版本变化只影响相关 REQ。旧项目不根据 confirmed、旧绿色测试或文件存在批量补 passed。先引用并复核已有材料；没有标准回执/逐条证据的是 not_assessed，含义是尚未评估而非功能未实现。补档不是重新确认产品，也不是要求重写代码。

## 工程包内容验收

project-development 在既有版本目录维护 ENGINEERING-PACKAGE.md 与 ENGINEERING-PACKAGE.json。内容清单包含 requirement_id/version、独立 package_revision、六类设计摘要及对应文档、每个 AC 的设计位置、WP、测试计划和所需证据类型。schema 与填写示例见 project-development 的 references/stage-delivery.md。

PM 复核身份、覆盖、工作包和内容回执。开发方实际审阅规格/工程设计是否一致；脚本只能验证引用和结构，不能证明自然语言内容正确。空模板不能签收。发现内部设计可逆缺口时继续自主补齐；真实产品取舍才回到 PM。已确认产品契约保持锁定，工程设计细节在工程包修订；改变行为/权限/数据含义/验收标准必须受控变更。

## 受控写入操作

均使用 pm.py apply，带有效 source_ref；除 configure_delivery 外，还要 requirement_id、expected_version。操作不会升级需求版本、解锁范围或改动确认来源。操作格式的 executable fixture 见 tests/test_delivery_workflow.py。

- `record_coverage`：rows 每行 ac_id、implementation=not_started/partial/complete、frontend=missing/partial/complete/not_required、verification=not_run/passed/failed/blocked。complete 要 code_refs 和 entry_points；frontend 不适用要 frontend_reason。验证通过还要 test_refs 和 evidence_ids。代码/测试文件内容自动固定指纹；不能写 acceptance。
- `review_stage`：stage 为表中 ID；result=passed/failed/blocked；必须有 reviewed_by、handoff_from、handoff_to（不同逻辑角色）、conclusion、next_action、artifact_refs。passed 必须带本阶段每项 checks 的 result=passed/not_applicable、reason 和 artifact_refs。两个角色可由同一 agent 先后承担，须实际进行接收方复核；无需为了签收自动创建其他 agent/聊天。
- `record_delivery_gap`：gap 记录稳定 id、stage、kind（product_decision/engineering/implementation/verification/external/acceptance）、status=open/resolved、reason、owner、action、target、done_when；resolved 要 artifact_refs。保留修订历史。未关闭缺口只阻止其所属阶段通过，不阻止独立 REQ。

阶段 checks 的实际必需键以 scripts/delivery_workflow.py 的 CHECKS 为准；检查脚本或引用此表填写，不发明“全部通过”总字段。

回执绑定规格指纹、需求版本、工程设计/实际源码/证据指纹、上游回执、范围配置。相关内容变化会派生 needs_review，不删除原回执；无关全局 revision 变化不使有效证据全部失效。verification passed 合法推进 ready_for_acceptance，保留 confirmation_lock 和需求版本，不再走 gate-only readiness 修改。acceptance 不改变确认状态或自动发布。

## 验证与验收证据

passed 的 EVD 需要 delivery 对象：requirement_id/version、ac_ids、command（人工场景可写执行步骤）、environment、executed_at（UTC ISO）、exit_code=0、mode=automated/real_external/manual/simulated，以及 artifact_fingerprints、code_fingerprints、test_fingerprints 三组项目相对路径到 SHA-256。人工验证的 test_fingerprints 指向场景步骤文件。必须来自实际执行；不可给旧结果换个标签冒充当前执行。

工程清单为每个 AC 指定 required_evidence。需要 real_external 的场景不能仅凭 simulated 通过。无法获取真实服务时明确受限项，继续独立工作，不宣称完整技术验证通过。

产品验收通过还需要：source.kind=user_message/user_confirmation/acceptor_decision，source.acceptance_scope 明确对应 REQ 版本，review_stage.acceptance_statement 记录明确验收原文/决定；acceptor_decision 必须来自项目已经指定有权验收者。AI 自测不能自建“人工验收来源”。需求封闭指令没有产品验收含义。

## 查询、汇总与停止条件

```text
pm.py --project-root ROOT workflow --requirement REQ-ID
pm.py --project-root ROOT workflow --stage engineering --requirement REQ-ID --strict
pm.py --project-root ROOT check --stage verification --requirement REQ-ID --strict
pm.py --project-root ROOT check --stage acceptance --requirement REQ-ID --strict
pm.py --project-root ROOT workflow --write-report
```

workflow 报告全部已纳入范围的阶段、每条 AC 和已知缺口。显式阶段检查不通过返回非零；not_assessed、needs_review、blocked 都不能算 passed。structure 检查只检查结构，不能宣称产品完成。按 REQ 过滤的结论只覆盖该 REQ，写报告始终生成全范围视图。

DELIVERY-WORKFLOW.md 与概览、resume 都从同一状态生成。每轮报告：本期需求和 AC 总数；完整/部分/尚未评估的实现数；验证通过/失败/未执行/受阻/待复核数；已知缺口数与尚未评估数；当前阶段、AI 下一动作及确实需要的外部决定。不能把 115 个待评审阶段说成 115 个产品缺陷。

阶段通过后自动继续已授权的下一阶段。遇到缺口先分类并修复；只有新增产品决定、必要外部输入、明确暂停或本期所有阶段交付完成时才停止受影响工作。最终验收通过不自动授权提交、推送、部署或发布。
