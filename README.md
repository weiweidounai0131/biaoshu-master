# biaoshu-master

面向政企类标书的分阶段协作与交付工作流，适用于政府部门、事业单位、国企、央企及其他政企客户的技术服务、咨询运营、信息化建设、系统集成、运维保障、培训实施和采购服务项目。

## 能力范围

- 在入口明确区分背景资料与参考资料：背景资料约束项目事实、评分、数字、时限和承诺；参考资料只提供经核对后的策略与表达参考。
- 通过本地确认台锁定项目口径、评分响应、目录页数、图片规划和最终Word批次。
- 按用户确认的1至5个批次生成、审阅和校正Word标书，执行篇幅、结构、格式和事实边界检查。
- 固定交付1个图片规划Excel，并为每张图片提供与已确认规划一致的`AI生图提示词`。
- 最终交付汇总后，仅在用户明确回复“继续”时进入示例图确认和剩余图片分批生成流程。
- 使用本地文件协议记录确认回执、图片请求、结果摘要和可恢复状态。

## 安全边界

G0至G7确认、正文生产和最终交付阶段默认不生成、不插入图片。图片规划Excel不包含成图路径、二进制或伪造结果。最终交付后的可选生图只接收已确认的图片规划、逐图提示词、统一视觉方向和用户的视觉修改要求；不得把背景资料、参考资料、Word正文或未确认的项目事实推送给生图模型，也不得自动将图片插入Word。

本技能只在用户显式引用`$biaoshu-master`、`biaoshu-master`、技能路径或明确要求使用主标技能时启用。每次显式调用的第一步执行：

```bash
python3 scripts/check_skill_update.py
```

发现GitHub新版本后先询问用户是否更新；只有用户明确同意且工作区干净时才执行`--update`。更新完成后重新显式调用技能并重新提交需求，避免新旧协议和项目回执混用。

更新检查兼容两种安装方式：Git仓库安装直接比较远端提交；ZIP安装通过`skill-update.json`定位GitHub仓库并比较技能包内容。ZIP安装包没有`.git`并不等于未配置远端。旧版ZIP若尚未包含此兼容版检查脚本，需要先安装一次包含该机制的新包，之后即可通过GitHub检查和更新。

## 运行入口

```bash
python3 scripts/prepare_intake.py <project_dir> \
  --background "项目背景" \
  --background-path "/绝对路径/需求书.docx" \
  --reference-path "/绝对路径/历史项目.docx"
python3 scripts/bid_confirm_ui/server.py <project_dir> --daemon --wait --wait-stage intake --wait-timeout 0
```

完整阶段规则、确认回执和最终交付后生图步骤见[`SKILL.md`](SKILL.md)及[`references/post-delivery-image-generation.md`](references/post-delivery-image-generation.md)。
