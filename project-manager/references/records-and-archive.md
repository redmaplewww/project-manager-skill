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
│  └─ DECISIONS.md                     # 自动生成决定与评审视图
└─ local/project-manager/
   ├─ conversations/*.jsonl            # 本机对话归档，不入 Git
   └─ search-index.json                # 可重建本机索引
```

`state.json` 保存细化状态，五本账本保存项目级当前结论并引用详细文档。不要手工编辑自动生成视图。既有项目可以映射已有文档，不自动迁移或重编号。

## 写入契约

所有改变通过 `pm.py apply --changes <file>`。变更文件包含 `expected_revision` 和 `operations`。工具执行乐观版本检查、状态转换检查、引用检查和原子写入，然后重建视图。失败不更新 revision。

操作包括：

- `set_project`：修改项目阶段、当前模式、名称、目标和时区等项目字段。
- `add`：向受支持集合加入带稳定 ID 的记录。
- `revise`：带 `expected_version` 修改条目并自动升级版本。
- `transition`：带预期旧状态改变需求、工作包、里程碑或风险状态。
- `add_source_ref`：给既有条目增加来源，不改变语义版本。

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
