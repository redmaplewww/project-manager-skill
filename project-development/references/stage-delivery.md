# 工程阶段验收与完整交付

与 project-manager 的 references/delivery-lifecycle.md（DELIVERY-1）配合。配置、回执、覆盖和缺口通过 PM apply 存入既有 state.json，不另建开发状态文件；工程文档和证据仍存于 canonical docs/project-development。

## 接收需求与交出设计

恢复当前版本、确认来源、工程包、WP、代码与证据，读 PM workflow/doctor。旧材料复核后复用；没有标准回执不表示代码未实现。接收 specification 回执，自动补齐工程材料，完成 engineering 内容评审和签收。尚无 ready_for_build 不影响调查产包；代码未实现和测试未执行属于后续工作。新增行为/权限/成本/验收取舍才回 PM。已有开发授权时自动接续实现，不在产包后停止。

## 工程包内容清单

版本目录维护 ENGINEERING-PACKAGE.md（详细设计）及 ENGINEERING-PACKAGE.json（设计清单），同步索引。JSON 不保存进度；同需求版本内递增 package_revision，产品行为变化才升级需求版本。

```json
{
  "requirement_id": "REQ-001", "requirement_version": 1, "package_revision": 1,
  "design": {
    "architecture": {"summary": "实际边界与调用链", "document_refs": ["docs/实际工程文档.md"]},
    "interfaces_data": {"summary": "实际契约、所有权、权限与失败", "document_refs": ["docs/实际工程文档.md"]},
    "frontend": {"summary": "实际入口、配置、交互与错误恢复；不适用说明依据", "document_refs": ["docs/实际工程文档.md"]},
    "execution": {"summary": "真实WP、依赖和步骤", "document_refs": ["docs/实际工程文档.md"]},
    "verification": {"summary": "实际验证场景和方法", "document_refs": ["docs/实际工程文档.md"]},
    "rollback": {"summary": "实际恢复策略或不适用依据", "document_refs": ["docs/实际工程文档.md"]}
  },
  "traceability": [{"ac_id": "AC-001", "design_section": "具体设计位置", "work_package_ids": ["WP-001"],
    "test_plan": "实际输入、操作、期望、反例和环境", "required_evidence": "automated"}]
}
```

document_refs 为项目根相对文件路径。每个 AC 恰好一行；WP 关联本 REQ、未取消、kind=implementation/verification/foundation，具有负责人、下一动作和完成条件。已有共享契约继续引用，避免重复。required_evidence=automated/real_external/manual/simulated，按规格选择；真实外部质量/工具链不能用模拟证据降级。脚本检查引用与覆盖，接收方仍须实际复核内容。

## 实现、证据与签收

纵向切片逐步交付，整个 REQ 以全部本期 AC 判断完整实现。后端存在但缺前端为部分实现；配置、迁移、失败恢复与跨模块消费都属于实现，不推给人工验收。

record_coverage 行包含 ac_id、implementation=not_started/partial/complete、frontend=missing/partial/complete/not_required、verification=not_run/passed/failed/blocked。complete 要 code_refs 和 entry_points；前端不适用要 frontend_reason。验证通过还要 test_refs 与 evidence_ids。可先完整实现、后独立验证，未执行不写 passed。

EVD 的标准 delivery 字段见 PM DELIVERY-1：requirement_id/version、ac_ids、command、environment、executed_at、exit_code=0、mode、artifact_fingerprints、code_fingerprints、test_fingerprints。指纹为项目相对路径到 SHA-256，固定实际执行时的输入；人工验证固定操作步骤与结果文件。代码/测试变化后重跑受影响验证，不能给旧结果贴上新源码指纹。旧证据复核后可引用；版本适用性无法证明则保持待评估，不伪造命令和执行时间。

review_stage 使用 requirement_id、expected_version、source_ref、stage、result、reviewed_by、handoff_from、handoff_to、conclusion、next_action、artifact_refs、checks。各 checks 的 result/reason/artifact_refs 来自真实复核；必需键以 PM scripts/delivery_workflow.py 的 CHECKS 为准。

- implementation passed：全部 AC 完整实现，前端完整或有不适用依据。
- verification passed：实现仍有效，逐 AC 所需类型的当前证据通过，接口/系统/UI/运行/源码审计完成复核。合法推进 ready_for_acceptance，保留确认锁和需求版本。
- acceptance：交 PM/指定验收者。AI 可执行用户场景，自测和需求确认不能代替产品人工验收。

工程修订与锁定产品契约分开；改变行为/权限/数据含义/验收标准才走 PM 范围变更。开发规范、接口和源码审计仍按原有 references 执行。

## 缺口与持续推进

record_delivery_gap 记录稳定编号、阶段、原因、执行者、位置、补齐动作和关闭条件。工程缺口自主处理，新增产品取舍交 PM，外部阻塞只影响相关项，继续其他已授权工作。check --stage implementation/verification --requirement REQ-ID --strict 用于阶段核验；未评估、待复核、失败或受阻都不算通过。

每轮报告分母、完整/部分/尚未评估实现、验证通过/未执行/失败/受阻、已知缺口与未知覆盖、下一自主动作及真正需要的输入。完整产品还要跨模块用户旅程验证，单模块通过不能冒充整个产品集成通过。
