# project-manager skill（项目经理）

让 AI 持续管理项目：从原始诉求、稳定需求和产品取舍，推进到工程交接、实现、验证与产品验收。确定性工具 `pm.py` 管理稳定 ID、需求版本、确认锁、阶段回执、证据指纹和原子写入。

本仓库同时维护配套的 `project-to-act` 和 `project-development` 快照，安装三项即可使用完整的管理与工程交接流程。

## 仓库结构

```text
project-manager/       # 需求、决策、确认、开发门禁及 DELIVERY-1 阶段回执
project-to-act/        # 治理底座：五本账本、文档索引、发现/初始化/校验/归档
project-development/   # 工程调查、工程包、实现、验证及源码审计
skills-manifest.json   # 本仓库的依赖与随包内容声明
requirements.txt      # Python 运行依赖
pm-sandbox/           # 历史 10 轮实测资料，非当前版本的验收证据
```

## 依赖声明

| 技能 | 关系 | 用途与条件 |
|---|---|---|
| [project-manager](project-manager/SKILL.md) | 主技能，随包提供 | 管理需求身份、产品范围、确认、就绪门禁及验收结论 |
| [project-to-act](project-to-act/SKILL.md) | 必需，随包提供 | 提供治理配置和唯一权威账本；先发现已有配置，再按授权初始化 |
| [project-development](project-development/SKILL.md) | 工程交接必需，随包提供 | 确认回答、封闭或版本变化后的工程调查与产包；产品实现沿用已有开发授权和门禁 |

`project-to-act` 可独立使用；`project-development` 也接受项目已有的批准规格，不强制创建 PM 状态。三者集成时共享现有治理源，不建立平行状态文件。

Figma 设计/实现、Web 应用测试、Playwright、静态视觉稿及演示文稿属于**按场景启用的可选能力**，未随包分发。完整名称、触发条件、工具前提和降级方式见 [技能依赖与可选集成](project-manager/references/skill-dependencies.md) 及 [协作路由](project-manager/references/external-skill-routing.md)。

[skills-manifest.json](skills-manifest.json) 是本仓库自定义的可读依赖清单，不是 Codex 原生的自动安装协议。技能依赖不写入只支持 MCP 工具声明的 `agents/openai.yaml` 的 `dependencies.tools`。

## 安装或更新

需要 Python 3.10 或更新版本、Git，以及支持 Agent Skills 的客户端。运行工具使用 Python 标准库；缺少系统 IANA 时区库的平台需要 `tzdata`，Windows 已通过环境标记列入运行依赖。

克隆到普通目录，再把三个技能目录复制到客户端实际扫描的技能根目录。不要只把仓库作为一个嵌套目录放进去。

```powershell
git clone https://github.com/redmaplewww/project-manager-skill.git
Set-Location project-manager-skill
python -m pip install -r requirements.txt

# Codex 默认位置；若设置了 CODEX_HOME 或客户端使用其他位置，请替换。
$skillRoot = Join-Path $env:USERPROFILE '.codex\skills'
New-Item -ItemType Directory -Force -Path $skillRoot | Out-Null
foreach ($name in @('project-to-act', 'project-development', 'project-manager')) {
    Copy-Item -LiteralPath $name -Destination $skillRoot -Recurse -Force
}
```

更新时，在本仓库运行 `git pull --ff-only` 后重新执行依赖安装与三目录复制。若目标技能有自定义改动，先保留备份并合并差异。客户端使用 `~/.agents/skills/` 时，将目标替换为该目录；无需同时安装两份。

## 快速使用

在本仓库目录执行以下命令；安装后可将脚本路径换成技能的实际安装路径。

```powershell
# 先发现现有治理配置；已有配置时保持原账本与文档根目录。
python -X utf8 project-to-act/scripts/init_project_management.py --project-root '<项目根>' --check

# 仅在项目尚未配置且需要初始化时执行。
python -X utf8 project-to-act/scripts/init_project_management.py --project-root '<项目根>'
python -X utf8 project-manager/scripts/pm.py --project-root '<项目根>' init --project-id '<ID>' --name '<名称>'

python -X utf8 project-manager/scripts/pm.py --project-root '<项目根>' inspect
python -X utf8 project-manager/scripts/pm.py --project-root '<项目根>' resume
python -X utf8 project-manager/scripts/pm.py --project-root '<项目根>' doctor
python -X utf8 project-manager/scripts/pm.py --project-root '<项目根>' workflow

# 结构化增量写入，遵守当前 revision、版本与确认锁。
python -X utf8 project-manager/scripts/pm.py --project-root '<项目根>' apply --changes changes.json
```

默认状态保存在 `<项目根>/.project-to-act/docs/project-manager/state.json`，并生成项目概览、需求、交付、决策、模块交互、开发指引和阶段工作流视图。现有 canonical docs 配置优先；不手工编辑生成视图。会话归档保存在不入 Git 的 `.project-to-act/local/`。

## 当前流程

- 原始诉求、产品需求与工作包分开保存，使用稳定 ID 和增量版本。
- 每个 REQ 独立完成产品、体验、技术、实现和验收审查；确认包回答与需求级封闭分别记录。
- 已确认范围缺少工程材料时自动补齐调查、工程包和索引，真正新增的产品取舍回到需求决策。
- DELIVERY-1 分别管理规格、工程交接、完整实现、技术验证与产品验收；内容回执、逐 AC 覆盖和证据保持在同一版本。
- 文档存在、结构校验通过或局部测试通过，只能证明对应范围，不代表完整产品验收。

## 验证

2026-11-07：65 项回归测试全部通过；三个技能的格式、依赖清单和文档链接校验通过；临时项目完成治理初始化、PM 初始化、inspect/resume/doctor/workflow 与严格结构检查。

```powershell
python -X utf8 -m unittest discover -s project-manager/tests -v
```

测试覆盖需求治理、确认锁、工程交接诊断、阶段回执、覆盖率、证据新鲜度与验收边界。历史角色扮演报告保留在 [pm-sandbox](pm-sandbox/)，不用于声称当前版本已经通过真实项目验收。
