# 安装与运行依赖

本技能的完整基础功能只依赖开源 Python 库，不依赖 Codex、WorkBuddy、任何模型厂商 API、Node 或内部 npm 包。

安装 skill 后，调用它的 AI 必须先执行：

```bash
python3 scripts/install_dependencies.py
```

脚本只执行用户级安装。遇到 Homebrew 等 PEP 668 受管理 Python 环境时，会自动追加官方兼容参数重试，不修改系统级 Python 文件。

脚本默认使用清华 PyPI 镜像，无需代理，安装：`python-docx`、`openpyxl`、`xlrd`。可先用 `python3 scripts/install_dependencies.py --check` 检查环境。

其中，Word 导出与审校使用 `python-docx`；图片规划 Excel 使用 `openpyxl`；旧式 `.xls` 评分表读取使用 `xlrd`。Node 与 `@oai/artifact-tool` 仅保留为显式增强选项，绝不构成工作流依赖。
