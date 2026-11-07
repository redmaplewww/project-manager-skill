# 技能依赖与可选集成

## 随包技能

本发布包以同级目录提供以下技能；安装完整工作流时一起安装。依赖清单见仓库根目录 `skills-manifest.json`，它是仓库自定义格式，不提供自动安装。

| 技能 | 依赖类别 | 使用边界 |
|---|---|---|
| `project-to-act` | 必需治理底座 | PM 初始化前先发现/配置权威账本；保留已有治理模式与 canonical docs 根目录 |
| `project-development` | 工程交接时必需 | 需求确认回答、封闭或版本变化后自动调查并维护当前版本工程包；产品编码继续遵守实施授权和开发门禁 |

缺少必需技能时，明确报告缺失项，继续可独立完成的调查；不得伪造账本配置、工程交接回执或已完成状态。项目只做讨论时，不为安装了配套技能而启动产品开发。

`project-to-act` 本身可独立维护治理文档；`project-development` 可采用外部批准规格。它们集成 PM 时加载 PM 的状态写入与 DELIVERY-1 协议，这属于条件协作，不要求独立使用时创建 PM 状态。

## 按需可选技能

以下能力不随本仓库分发，也不是启动 PM 的前置条件。只有交付物确实需要时才使用，具体上下文和证据回收按 [外置 Skill 协作路由](external-skill-routing.md)。

| 技能名称 | 启用条件 | 额外前提或缺失时的处理 |
|---|---|---|
| `figma-generate-design` + `figma-use` | 需要可编辑的 Figma 页面或设计系统 | Figma 工具连接和目标文件；写入前加载 `figma-use`；不可用时用文本线框/Mermaid 表达结构 |
| `figma-implement-design` | 将已有 Figma 设计转为代码 | Figma URL/节点及 MCP；按实现技能要求加载 Figma 前置技能；缺失时先保留设计差异和可调查工程事实 |
| `webapp-testing` | 验证本地 Web 应用 | 可运行应用及浏览器测试环境；不可用时执行可用检查并如实标记浏览器交互未验证 |
| `playwright` | 浏览器调试、表单流程或跨页面验证 | 浏览器自动化环境；缺失时保留复现步骤，不伪造截图或操作证据 |
| `canvas-design` | PNG/PDF 概念或展示稿 | 对应绘图/字体环境；可先用可编辑图表说明产品结构，不将概念稿当功能验证 |
| `presentations`（客户端也可能显示为 `presentations:Presentations`） | 用户需要评审演示文稿 | 演示文稿工具运行时；不可用时交付可审阅的内容大纲并明确文件尚未生成 |

原生 `agents/openai.yaml` 的 `dependencies.tools` 只用于支持的 MCP 工具依赖，不能用来声明这些 skill 名称。可选技能需要的服务连接按其自身说明设置，PM 不隐式创建连接或扩大外部操作授权。

## 运行环境

Python 3.10+。运行脚本使用标准库；Windows 安装仓库 `requirements.txt` 中的 `tzdata`，其他没有 IANA 时区数据库的平台也需安装它。测试使用标准库 `unittest`。若另行使用 `skill-creator` 的 `quick_validate.py`，其验证环境需要 `PyYAML`，这不是 PM 的运行依赖。
