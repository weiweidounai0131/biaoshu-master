# Biaoshu Master

![BID MASTER — From brief to bid-ready deliverables](assets/branding/bid-master-banner.png)

> 把需求、规则和项目资料，变成有依据、有结构、可审阅、可交付的正式标书。
>
> A local-first, AI-assisted proposal workflow for turning briefs and source materials into bid-ready deliverables.

Biaoshu Master 不是一段“把文字写长”的提示词，而是一套面向正式投标与方案竞标的分阶段协作工作流。它运行在具备文件读写、命令执行和持续对话能力的 AI 工具中，负责把资料理解、项目口径、评分响应、目录篇幅、正文生产、Word 导出、AI 复校和人工确认串成一条可追溯的交付链。

它不局限于政企项目。政府与公共服务、通信与信息化、软件与 SaaS、系统集成、云服务、工程建设、制造与能源、医疗与教育、咨询与运营、采购与培训，以及其他需要正式方案和投标文件的行业场景，都可以使用同一套底层流程；具体表达、规则和交付边界由项目资料与所选生成规则决定。

## 为什么是 Biaoshu Master

- **先建立事实，再开始写作**：需求书、招标文件、评分表和客户正式资料作为背景底座；历史项目与成熟方案只作为经过核对的可选参考，不能悄悄变成当前项目事实。
- **先形成结构，再组织正文**：项目目标、评分响应、章节层级和页数预算在写作前锁定，正文围绕已确认的一至三级骨架深度展开。
- **把 AI 复校放到人工审阅之前**：每批 Word 导出后，当前 AI 必须重新读取项目规则，对事实边界、重复段落、章节承接、评分响应、跨批一致性、Word 结构和篇幅进行独立复核；存在阻断问题时不能直接交给用户确认。
- **本地优先，宿主中立**：确认台、交付台和审校回执保留在本机；Codex、WorkBuddy 或其他具备本地文件能力的 AI 都可以驱动同一套文件协议，不锁定某一家模型或平台。

## 你会得到什么

- 1 至 5 个按章节拆分的 Word 标书文件，数量以 Stage4 确认结果为准；
- 1 个图片规划 Excel，包含位置、用途、核心节点、构图建议和逐图 AI 生图提示词；
- 项目级确认回执、源稿摘要、导出校验和 AI 复校记录，便于发现问题、恢复中断和追踪修改；
- 可选的行业生成规则：默认规则、预设规则和用户确认后写入本机的自定义规则。

## 工作流

```text
资料入口 → 项目口径 → 评分与框架 → 图片规划 → 最终授权
                                      ↓
                         Word 生产 → AI 复校 → 人工审阅 → 最终交付
```

每个大阶段都有明确的确认闸门，不会因为“已经规划好了”就自动跳过用户确认。阶段4生产与审校台支持逐批阅读、直接修改、AI 回修请求、重新导出、页数复核和最终锁定。

## 适用范围

Biaoshu Master 适合但不限于：

- 通信、网络、安全、数据、软件、SaaS、云平台和系统集成；
- 工程建设、基础设施、能源、制造、园区和物业服务；
- 政府、事业单位、国企、学校、医院及其他组织的采购与服务项目；
- 咨询、运营、培训、营销、会展、人力、外包和综合服务；
- 任何需要依据招标文件、评分标准或客户需求编制正式方案的行业项目。

## 重要边界

这是一套可靠的工作流，不是“无需判断的自动许愿机”。模型能力会影响最终文字质量，Skill 负责把事实、结构、规则、复校和交付闸门固定下来，但不会替用户发明业绩、人员、证书、客户授权、指标或其他项目证据。

页数预算用于组织和发现正文偏薄，最终页数仍应以目标办公软件中的实际打开结果为准。G0 至 G7 默认只生成 Word 和图片规划 Excel，不生成或插入图片；最终交付后的本机生图属于单独的、需要用户明确回复“继续”的流程。

## 快速开始

将 Skill 解压或克隆到宿主支持的技能目录，在当前目录安装依赖：

```bash
python3 scripts/install_dependencies.py
```

首次进入项目，先解析项目工作区：

```bash
python3 scripts/project_workspace.py resolve \
  --project-name "项目名称" \
  --client "招标人或客户"
```

然后在 AI 对话中显式调用 `$biaoshu-master`，并提供项目背景、背景资料路径和可选参考资料路径。确认台会按顺序引导项目口径、标书框架、图片规划和最终授权；确认后的正文生产由独立的本地生产与审校台承接。

只有当前消息明确引用 `$biaoshu-master`、`biaoshu-master` 或技能路径时，本 Skill 才会启用；单独讨论标书不会自动触发。

## 项目工作区与生成规则

项目工作区、Skill 安装目录和最终交付物保存目录彼此分离。相同项目再次进入时复用稳定工作区，不同项目创建不同工作区；重新开始一轮时归档旧确认状态和交付区，避免新旧项目混用。

Stage4 默认使用通用规则，也可以选择领域预设。用户可在确认台复制“请用 biaoshu-master 技能，帮我制作专属的标书生成规则”，回到当前对话完成规则设计；只有得到明确确认后，规则才会写入本机 Skill 并可设为后续默认。默认规则不会被覆盖。

## 界面与交付台

下面的展示图按实际页面顺序整理：资料入口、项目口径、标书框架、图片规划、最终确认、生产与审校。截图只展示通用界面，不含项目交付资料。

![Biaoshu Master workflow gallery](assets/screenshots/workflow-gallery.png)

## 更新检测与双平台发布

本 Skill 使用 `skill-version.json` 保存本地版本，按 GitHub Contents API、GitHub `refs/heads` 原始文件、GitCode Contents API 的顺序检查远端版本；GitHub 不通时自动切换 GitCode。更新检测不依赖本地 `.git`，Git 安装和 ZIP 安装使用同一套逻辑：

```bash
python3 scripts/check_skill_update.py
```

检测到远端版本更高时，先向用户询问是否更新；只有用户明确同意后才执行：

```bash
python3 scripts/check_skill_update.py --update
```

本 Skill 的 canonical 源码同时维护在 [GitHub](https://github.com/weiweidounai0131/biaoshu-master) 和 [GitCode](https://gitcode.com/gcw_mHRylKw0/biaoshu-master)。发布更新时两个平台使用同一个提交，并分别核对远端分支；项目资料、本机运行数据和凭据不会进入公开仓库。

## 隐私与公开边界

公开仓库只包含通用 Skill 代码、规则、文档和脱敏界面截图。项目背景、招标文件、参考资料、确认回执、Word/Excel 交付物、本机运行数据、绝对路径和凭据都保存在本地项目目录，不会被 Skill 自动上传或复制到公开仓库。

## 目录结构

```text
biaoshu-master/
├── SKILL.md
├── README.md
├── skill-version.json
├── skill-update.json
├── agents/
├── assets/
├── references/
├── rules/
└── scripts/
```

详细的阶段协议、正文规则、生产审校和最终交付后的可选生图流程见：

- [SKILL.md](SKILL.md)
- [确认台协议](references/confirmation-ui.md)
- [生产与审校台协议](references/production-review.md)
- [Stage4写作规则](references/stage4-writing-rules.md)
- [最终交付后生图协议](references/post-delivery-image-generation.md)

项目地址：[github.com/weiweidounai0131/biaoshu-master](https://github.com/weiweidounai0131/biaoshu-master)
