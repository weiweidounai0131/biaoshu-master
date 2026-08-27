# 最终交付后生图协议

## 目的

本协议定义最终交付完成后的可选本机生图流程。它不是标书确认台的阶段，也不改变G0至G7的确认、正文、Word或图片规划Excel边界。

生图只能由用户显式授权触发：当前AI完成最终交付汇总后只追加一次“是否调用本机生图模型生成图片？回复‘继续’开始；回复‘否’或不回复则不生成图片。”只有明确回复“继续”才可启动；沉默、模糊回复或其他内容都不能视为授权。

## 输入隔离

生图模型只接收：

- 已通过最终确认的图片规划Excel；
- 图片规划结构化源稿中的一条或一批图片记录；
- 对应的`ai_prompt`；
- 已确认的统一视觉方向；
- 用户针对示例图提出的视觉修改要求。

不得推送背景资料、参考资料、Word正文、未确认的项目事实、客户隐私、人员信息、材料原文或本机运行日志。`ai_prompt`必须只描述规划中已确认的表达任务，不能把参考资料中的案例、客户、人员、证书、业绩、数字、日期、Logo、品牌或承诺变成图片内容。

## 状态机

状态保存在`<project_dir>/bid_delivery/image-generation/`，包括：

| 状态 | 含义 | 可执行动作 |
| --- | --- | --- |
| `example_pending` | 已获得“继续”，等待首张示例图 | 生成或重新生成示例图 |
| `example_ready` | 示例图已落盘，可供用户查看 | 修改示例图或确认 |
| `awaiting_batch_count` | 用户已确认示例图，等待选择批次 | 选择1至5次 |
| `generating` | 剩余图片正在按批次生成 | 记录某一批结果 |
| `complete` | 示例图及全部批次均已记录 | 只读查看 |

请求和结果均写入JSON，并绑定`project_id`与`final_confirmation_sha256`。如果最终确认回执或图片规划发生变化，旧生图状态不得继续使用，需重新完成最终确认和显式授权。

## 交互顺序

### 1. 示例图

用户回复“继续”后运行：

```bash
python3 scripts/bid_delivery_ui/image_generation.py <project_dir> start-example
```

当前AI读取返回的请求文件，将第一条图片规划和`ai_prompt`交给本机生图模型，立即只生成这一张示例图。示例图用于确认整体视觉风格，不得顺便生成其他图片，也不得改写或插入Word。

模型生成后，使用本机绝对路径登记结果：

```bash
python3 scripts/bid_delivery_ui/image_generation.py <project_dir> record-example "/绝对路径/示例图.png"
```

### 2. 修改或确认示例图

用户提出修改时运行：

```bash
python3 scripts/bid_delivery_ui/image_generation.py <project_dir> revise-example "用户的视觉修改要求"
```

当前AI必须根据用户要求调整示例图的视觉方向或`ai_prompt`，再重新调用本机模型生成示例图。修改请求未完成前不得进入批量生成。

用户回复“确认”后运行：

```bash
python3 scripts/bid_delivery_ui/image_generation.py <project_dir> confirm-example
```

然后询问：“接下来将剩余图片分几次生成？可回复1、2、3、4或5。”

### 3. 剩余图片分批生成

用户选择1至5中的一个整数后运行：

```bash
python3 scripts/bid_delivery_ui/image_generation.py <project_dir> set-batch-count 3
```

脚本将示例图之外的剩余图片按顺序拆成不超过用户选择次数的非空批次，并为每批写入请求文件。当前AI按请求文件逐批调用本机模型；每批严格使用自己的图片记录和提示词，不得补生成请求外的图片。

每批完成后登记结果：

```bash
python3 scripts/bid_delivery_ui/image_generation.py <project_dir> record-batch 1 "/绝对路径/图片1.png" "/绝对路径/图片2.png"
```

结果登记要求：路径必须是存在的本机PNG、JPG、JPEG或WEBP绝对路径；图片数量必须与该批请求完全一致；脚本记录SHA-256，避免生成结果被静默替换。

## 不允许的行为

- 在最终交付汇总前主动询问或调用生图模型；
- 把“否”、不回复或非明确“继续”当作授权；
- 将背景资料与参考资料发送给生图模型；
- 让图片模型自行补充客户、人员、业绩、数字、证书、Logo、品牌、日期或承诺；
- 生成后自动替换Word中的图片或改变已确认的图片规划；
- 本机模型不可用时无提示地切换到外部图片服务；
- 用旧最终确认回执启动新一轮生图。
