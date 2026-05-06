# Codex-Claude AutoFlow

[English README](./README.md)

Codex-Claude AutoFlow 是一个面向本地代码工程的 Windows 图形界面工具，核心目标是实现 **Codex + Claude 全自动协作**。

你只需要提供：

- 工程目录
- 一个或多个允许修改的代码目录
- 任务标识
- 原始任务描述

工具就会自动执行这条结构化链路：

1. `Codex` 读取你的原始任务并生成实现计划
2. `Claude` 只接收 `Codex` 的具体计划指令，然后直接修改真实工程代码
3. `Codex` 审查修改结果
4. 如果有问题，`Claude` 根据审查意见继续修改
5. `Codex` 进行终审
6. `Codex` 会在流程通过后默认生成一份收尾总结文档（含 commit 说明建议）
7. 如果你开启“可选真实提交”，`Codex` 会在 06 文档生成后尝试执行一次真实 git commit

这样整个改代码过程会更稳定、更可追踪，也更适合反复执行的本地工程任务。

## 核心行为

- 全自动双 Agent 协作链路：`Codex -> Claude -> Codex`
- 原始用户任务只会发送给 `Codex`
- `Claude` 不会直接读取你最初输入的任务描述
- `Claude` 只会根据以下内容执行修改：
  - `Codex` 生成的计划
  - `Codex` 给出的审查意见
  - 你选中的工程代码文件
- 当你选择一个工程目录时，如果该工程已经存在当前任务，界面会自动回填这个工程的活动 `workflow`

## 主要功能

- Windows 图形界面启动器
- 支持多个允许修改的代码目录
- 支持选择 Codex 模型
- 支持按阶段选择 Codex 推理强度（Plan/Review/Final/Wrap-up）
- 支持选择 Claude 模型
- 支持选择 Claude 权限模式
- 默认生成收尾总结文档（`06_codex_commit.md`）
- 支持可选真实 git commit（在 06 文档之后）
- 支持收尾阶段状态追踪
- 支持任务标识和任务 ID
- 当前任务状态保存在 `workflow/`
- 历史任务归档保存在 `workflow_history/`
- 可以在界面中恢复旧任务
- 支持终审未通过后的“再修再审”多轮循环
- 支持暂停后继续
- 所有工作流产物都保存在目标工程目录中

## 按钮逻辑

选择工程目录后，如果该工程已经有当前活动任务，启动器会自动加载这个工程的 `workflow/` 内容。这是预期行为，不表示它已经开始运行，只是把当前任务状态恢复到界面上。

几个主要按钮的作用现在是明确分开的：

- `开始运行`：只有在当前工程没有活动 `workflow` 时，才会直接启动一个全新的任务
- `继续上次任务`：继续当前活动任务
- `再修再审`：只用于终审被拒绝后的下一轮修改和再审
- `新任务草稿`：先把当前 `workflow` 归档到 `workflow_history/`，然后清空任务名称和任务描述，**但不会自动运行**

如果你要在一个已经有旧任务的工程里开启全新任务，正确操作是：

1. 选择工程目录
2. 点击 `新任务草稿`
3. 重新填写新的任务标识和新的任务描述
4. 再点击 `开始运行`

## 收尾总结行为

每次终审通过后都会生成 `06_codex_commit.md`：

- 只有在终审通过后才会进入收尾阶段
- Codex 会生成 `06_codex_commit.md`，内容包含：
  - 本次工作流总结
  - 文件改动摘要
  - 建议的 commit 标题与正文
  - 建议你手动执行的 git 命令
- 默认不会执行 `git add` / `git commit` / `git push`
- 阶段是否成功以文档内容是否生成成功为准

这意味着：

- 你仍然会得到统一的收尾产物
- 真正的 git 提交始终由你手动执行，可直接参考 06 文档中的建议

如果你勾选了“可选真实 git commit”：

- 在 06 文档生成后，Codex 会额外尝试执行一次真实提交
- 执行结果（成功/跳过/失败原因）会附加写入 `06_codex_commit.md` 末尾

## 只下载 EXE 能不能用

**可以。对普通使用者来说，只下载 `CodexClaudeWorkflow.exe` 就足够使用。**

你不需要 Python，也不需要源码文件，但前提是下面几项都满足：

- 你的系统是 Windows
- 已经安装 `codex` CLI
- `codex` CLI 已经登录可用
- 已经安装 `claude` CLI
- `claude` CLI 已经登录可用
- EXE 所在目录允许生成 `launcher_settings.json`

也就是说：

- **只要有 EXE，就可以运行这个程序**
- **这个 EXE 不包含 `codex` CLI**
- **这个 EXE 不包含 `claude` CLI**
- **只有在你要开发、调试、二次修改或重新打包时，才需要完整源码仓库**

## 快速开始

### 方式一：直接使用 EXE

1. 下载 `CodexClaudeWorkflow.exe`
2. 确保本机已安装并登录 `codex` 和 `claude`
3. 启动 EXE
4. 选择工程目录
5. 添加一个或多个允许修改的代码目录
6. 填写任务标识和任务描述
7. 点击 `开始运行`

如果你选中的工程已经有活动任务：

- 想继续旧任务，就点 `继续上次任务`
- 想新开任务，就点 `新任务草稿`，修改表单后再点 `开始运行`

### 方式二：使用源码运行

运行：

```powershell
python .\workflow_launcher.py
```

或者直接双击：

```text
open_workflow_launcher.cmd
```

## 仓库目录说明

主要源码文件：

- `workflow_launcher.py`：图形界面启动器
- `orchestrate_agents.py`：工作流编排脚本
- `run_workflow.cmd`：命令行入口
- `open_workflow_launcher.cmd`：图形界面入口
- `build_exe.py`：EXE 打包脚本
- `build_exe.cmd`：辅助打包命令
- `clean_local.py`：清理本地构建残留
- `clean_local.cmd`：辅助清理命令
- `selftest/`：用于冒烟测试的假 `codex` / `claude` 命令

常见本地/生成目录：

- `dist/`：打包后的 EXE 输出目录
- `build/`：PyInstaller 构建产物
- `_tmp/`：构建临时目录
- `__pycache__/`：Python 缓存
- `.venv_build/`：本地构建环境残留
- `_build_tools/`：可选的本地 PyInstaller 依赖缓存

## 任务历史与恢复

每个目标工程都会有一个当前活动任务目录：

- `workflow/`

当你在同一个工程下启动新的任务时：

- 旧的 `workflow/` 会被归档到 `workflow_history/`
- 新任务会成为新的当前 `workflow/`

GUI 支持：

- 查看当前任务
- 查看历史任务
- 把某个历史任务恢复成当前活动任务继续执行

如果终审没有通过：

- 工作流会进入等待状态
- 你可以先关闭程序
- 之后重新打开
- 再加载同一个工程
- 然后继续进行下一轮再修再审

## GitHub 提交建议

建议提交到 GitHub 的内容：

- 源码文件
- `README.md`
- `README.zh-CN.md`
- `LICENSE`
- `selftest/`
- `.gitignore`

建议不要提交到 GitHub 的内容：

- `build/`
- `_tmp/`
- `__pycache__/`
- `.venv_build/`
- `_build_tools/`
- 本地生成的 `launcher_settings.json`

`dist/` 更适合作为 **GitHub Release 附件** 发布，而不是直接提交进仓库历史。

推荐的发布方式：

1. 把源码仓库推到 GitHub
2. 不把本地构建残留提交进 git
3. 把 `CodexClaudeWorkflow.exe` 作为 GitHub Release 附件上传

## 清理本地构建残留

如果你想在发布前把仓库整理干净，可以执行：

```powershell
python .\clean_local.py
```

或者直接双击：

```text
clean_local.cmd
```

这个脚本会清理以下本地构建残留：

- `build/`
- `_tmp/`
- `.venv_build/`
- `_build_tools/`
- `__pycache__/`
- `*.pyc`
- `dist/launcher_settings.json`

它会保留已经打包好的 EXE 文件本身。

## 从源码重新打包

如果你要重新生成 EXE，可以执行：

```powershell
python .\build_exe.py
```

或者：

```powershell
.\build_exe.cmd
```

`build_exe.py` 支持两种方式：

- 使用本地 `_build_tools` 中的 PyInstaller
- 使用全局安装的 `PyInstaller`

如果本机还没有安装，可以先执行：

```powershell
pip install pyinstaller
```

## 许可证

本项目采用 GPL 许可证，详见 [LICENSE](./LICENSE)。
