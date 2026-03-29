# `watch_codex_sessions.py`

English README: [README.md](./README.md)

下面的命令示例默认假设这个工具目录本身就是仓库根目录。如果你把它嵌在别的仓库里使用，请把命令里的脚本路径改成你的实际路径。

`watch_codex_sessions.py` 用于把本机 Codex Desktop 的会话数据归档到本地可控目录中的 `output/codex-archive/`，同时提取 Plan 模式中的 `<proposed_plan>...</proposed_plan>` 为独立 Markdown 文档。

这个脚本的设计目标是：

- 回填历史会话
- 持续跟踪新增会话
- 默认使用增量发现与增量读取，避免反复全量扫描 rollout 目录
- 同时归档主线程和 subagent 线程
- 保留可见会话、工具调用、工具结果和调度事件
- 对执行计划做独立落盘、审计和保留校验
- 把归档输出限制在 `output/codex-archive/`，避免和源码或其他工作文件混在一起

## 适用范围

- 平台：Windows、Linux、macOS
- 实现：Python 标准库
- 默认 Codex 数据目录：
  - 优先读取环境变量 `CODEX_HOME`
  - 未设置时回退到 `~/.codex`

## 读取的数据源

脚本会组合读取以下本地文件：

- `~/.codex/session_index.jsonl`
- `~/.codex/sessions/**/*.jsonl`
- `~/.codex/archived_sessions/**/*.jsonl`
- `~/.codex/state_5.sqlite`

SQLite 状态库路径也可以通过参数覆盖：

- `--state-db-path /path/to/state.sqlite`

用途分工：

- `sessions` / `archived_sessions` 下的 rollout JSONL 是会话内容真相源
- `session_index.jsonl` 用于发现新的线程名称和新增线程
- `state_5.sqlite` 的 `threads` 表用于补齐线程标题、subagent 元信息和父子关系

## 增量机制

脚本现在分两层做增量：

- 读取层增量
  - 每个 session 都会保存 `last_offset`
  - 再次处理同一个 rollout 时，只从上次偏移继续读取新增内容
- 发现层增量
  - 首次运行或显式 `--rescan` 时，才递归扫描 `sessions/` 和 `archived_sessions/`
  - 常规运行时，优先依赖 `session_index.jsonl` 的新增尾部和 `state_5.sqlite` 的线程元数据发现新会话
  - 已知 rollout 路径会写入 `_state/manifest.json`，后续直接复用

这意味着：

- 第一次建档时会做一次全量发现
- 后续 `--backfill-only`、默认模式和 `--follow-only` 都会优先走结构化增量发现
- 如果你怀疑有漏发现，或者本地 Codex 数据目录发生了手动迁移，再用 `--rescan`
- 如果本轮没有任何实质性变化，脚本会尽量避免重写 `_state/sessions/*.json`、`transcript`、`manifest.json` 和 `thread_index.json`
- 最近一次运行到底走了全量还是增量，会写入 `_state/manifest.json` 和 `reports/archive-audit.md`
- 最近一次 SQLite 状态库读取是否成功、schema 是否兼容，也会写入这些状态文件

## 会归档哪些内容

会归档这些可见信息：

- 用户消息
- assistant 最终回复
- assistant commentary
- 工具调用
- 工具输出
- 调度事件
  - `spawn_agent`
  - `send_input`
  - `wait_agent`
  - `close_agent`
- reasoning 事件元数据

不会导出隐藏思考原文。`reasoning` 只记录事件存在性和是否含有加密载荷，不尝试解密或伪造 CoT。

## Plan 提取规则

脚本会从 assistant 消息中提取：

```text
<proposed_plan>
...
</proposed_plan>
```

提取规则：

- 优先要求当前 turn 处于 `collaboration_mode_kind == "plan"`
- 如果历史数据里能看到 `<proposed_plan>`，但无法确认 mode，仍会提取
- 计划标题优先取 plan 块内第一个 Markdown `# Heading`
- 如果没有 `# Heading`，则取第一行非空文本
- 文件名会自动清洗，移除如 `#`、反引号、Windows 非法字符等不规范内容
- 重名文件会自动追加 `-2`、`-3`
- 如果多个来源的计划正文完全相同，只保留 1 份 canonical `.md`
- 其它来源信息保存在 `_state/plans.json`，不再额外生成同正文副本
- 执行正文级去重时，如果只剩 `Title-2.md` 这类历史遗留文件且基础文件名空闲，脚本会自动回正为 `Title.md`

计划文件 front matter 会记录：

- `title`
- `session_id`
- `thread_name`
- `source_kind`
- `parent_thread_id`
- `agent_nickname`
- `agent_role`
- `source_turn_id`
- `plan_mode_confirmed`
- `plan_generated_at`
- `extracted_at`
- `source_rollout`

关于时间：

- `plan_generated_at` 取自生成该计划的会话事件时间
- Windows 上会尽力把计划文件的文件系统创建时间同步为 `plan_generated_at`
- Linux / macOS 不保证能修改 birth time，因此应以 front matter 中的 `plan_generated_at` 为准

## 输出目录结构

默认输出目录：

- `output/codex-archive/`

主要结构如下：

```text
output/codex-archive/
  thread_index.json
  _state/
    manifest.json
    plans.json
    sessions/<session-id>.json
  reports/
    archive-audit.md
    filename-audit.md
    retention-audit.md
  sessions/
    <session-id>/
      meta.json
      events/
        part-0001.jsonl
        part-0002.jsonl
      transcript/
        part-0001.md
        part-0002.md
  plans/
    <sanitized-title>.md
    <sanitized-title>.part-0002.md
```

其中：

- `meta.json` 保存线程级元数据
- `events/part-*.jsonl` 保存规范化后的事件流
- `transcript/part-*.md` 保存适合阅读的转录
- `thread_index.json` 保存主线程与 subagent 的父子关系
- `_state/` 保存增量跟踪和 plan 索引状态
- `reports/` 保存审计报告

`_state/manifest.json` 里还会记录最近一次运行的发现状态，例如：

- `last_discovery_mode`
- `last_full_scan_at`
- `last_incremental_scan_at`
- `last_discovered_source_count`
- `last_new_source_count`
- `last_processed_source_count`

## 大文件自动拆分

为避免归档文件持续变大，脚本会自动分片：

- `events` 默认每片最大 `32 MiB`
- `transcript` 默认每片最大 `16 MiB`
- `plan` 默认每片最大 `8 MiB`

对应参数：

```bash
python watch_codex_sessions.py --events-max-bytes 33554432 --transcript-max-bytes 16777216 --plan-max-bytes 8388608
```

如果检测到旧格式的单文件归档，脚本会在后续运行中自动迁移为分片格式。

## 常用命令

### 1. 回填全部历史会话

```bash
python watch_codex_sessions.py --backfill-only
```

### 2. 持续跟踪新会话

```bash
python watch_codex_sessions.py --follow-only
```

### 3. 先回填，再持续跟踪

```bash
python watch_codex_sessions.py
```

### 4. 打印可见运行日志

```bash
python watch_codex_sessions.py --verbose
```

### 5. 强制重新全量发现 rollout 源

```bash
python watch_codex_sessions.py --rescan --backfill-only
```

### 6. 校验已提取计划是否仍能从原始 rollout 中找到

```bash
python watch_codex_sessions.py --verify-retention
```

### 7. 审计历史会话是否都已归档完成

```bash
python watch_codex_sessions.py --audit-archive
```

### 8. 审计归档文件名是否规范

```bash
python watch_codex_sessions.py --audit-filenames
```

### 9. 修复不规范的动态文件名

```bash
python watch_codex_sessions.py --repair-filenames
```

### 10. 按计划正文去重并清理历史重复文件

```bash
python watch_codex_sessions.py --dedupe-plans-by-content
```

### 11. 指定 Codex 数据目录和输出目录

```bash
python watch_codex_sessions.py --codex-home ~/.codex --output-dir output/codex-archive
```

### 12. 指定自定义 SQLite 状态库路径

```bash
python watch_codex_sessions.py --state-db-path ~/.codex/state_5.sqlite --backfill-only
```

## `--auto-git` 自动提交与推送

脚本支持可选的归档自动同步：

```bash
python watch_codex_sessions.py --follow-only --auto-git
```

相关参数：

- `--auto-git`
- `--git-remote origin`
- `--git-commit-interval-seconds 300`

安全边界：

- 只会处理 `output/codex-archive/`
- 如果暂存区里已经有该路径以外的 staged 变更，脚本会拒绝自动同步
- 如果本轮没有归档变化，不会 commit 或 push

默认 commit message 形如：

```text
codex-archive: sync 2026-03-28T10:00:00Z
```

## `--verbose` 运行可见性

脚本默认是安静运行的。这对长期 `--follow-only` 监听很有帮助，但在交互式终端里容易让人误以为“没有反应”。

如果你希望看到实时进度，建议显式加上 `--verbose`：

```bash
python watch_codex_sessions.py --backfill-only --verbose
python watch_codex_sessions.py --follow-only --verbose
python watch_codex_sessions.py --follow-only --auto-git --verbose
```

启用后会打印：

- 启动时使用的 `codex_home`、`output_dir`、`poll_seconds`
- 本轮发现是 `full` 还是 `incremental`
- 本轮处理了多少 source
- 当前 `state_db` 状态
- auto-git 是 `disabled`、`no-changes`、`cooldown` 还是 `committed`
- 何时进入 watch 循环

## 审计报告说明

### `reports/archive-audit.md`

用于检查：

- 最近一次归档运行走的是全量发现还是增量发现
- 最近一次发现到了多少 rollout 源、其中多少是新增源
- 历史 active sessions 是否都已导出
- 历史 archived sessions 是否都已导出
- session state 的 `last_offset` 是否已经追平源 rollout 文件大小
- 是否存在孤儿归档
- `thread_index.json` 中主线程与 subagent 的父子关系是否完整

### `reports/filename-audit.md`

用于检查：

- 计划文件名是否包含不规范字符
- 路径组件是否含有 `#`、反引号、Windows 非法字符、尾部句点或多余空格

### `reports/retention-audit.md`

用于检查：

- 已记录的 `plan_hash` 是否仍能在原始 rollout JSONL 中重新定位
- 是否发生了“计划已经导出，但原始来源里找不到”的保留缺失

## Troubleshooting

### 执行后看起来没有任何反应

如果你直接运行默认模式，例如：

```bash
python watch_codex_sessions.py --output-dir output/codex-archive --auto-git
```

脚本会先处理一轮，然后进入持续 watch 循环。默认不打印日志，所以看起来可能像“没有反应”，但进程其实是正常挂起等待新变化。

建议这样排查：

1. 用 `python watch_codex_sessions.py --backfill-only --verbose` 看一次性的可见执行
2. 用 `python watch_codex_sessions.py --follow-only --verbose` 看持续监听时的实时状态
3. 查看 `output/codex-archive/_state/manifest.json`
4. 查看 `output/codex-archive/reports/archive-audit.md`

当前版本对 `manifest.json`、`plans.json`、session state、transcript 分片、plan 文件和审计报告都改成了“原子替换 + 短重试”写入，因此 Windows 上偶发的瞬时写入异常不太容易再留下半写入文件。如果只出现过一次类似报错，建议先直接重跑一次再判断是否存在持续性问题。

如果之前某次中断运行留下了全 0 的 `manifest.json`、`meta.json` 或 transcript 分片，重新执行一次 `--backfill-only` 通常就可以基于底层 rollout JSONL 重新生成这些文件。若回填完成后仍残留同目录下的 `*.tmp` 临时文件，可以再手动清理。

如果 SQLite 驱动的线程元数据缺失或质量下降，优先检查这些状态字段：

- `state_db_path`
  - 当前 watcher 实际使用的 SQLite 文件路径
- `last_state_db_status`
  - 最近一次状态库读取结果
  - 常见值包括 `ok`、`missing`、`schema_error`、`error`
- `last_state_db_schema_ok`
  - `threads` 表结构是否符合当前脚本预期
- `last_state_db_checked_at`
  - 最近一次检查状态库的时间
- `last_state_db_error`
  - 最近一次连接、查询或 schema 检查的错误信息

优先查看的位置：

- `output/codex-archive/_state/manifest.json`
- `output/codex-archive/reports/archive-audit.md`

常见情况：

- `last_state_db_status = missing`
  - 当前配置的 SQLite 文件不存在
  - 检查 `CODEX_HOME`，或显式传 `--state-db-path`
- `last_state_db_status = schema_error`
  - `threads` 表不存在，或者列结构发生变化
  - 此时 rollout 解析仍可继续，但线程元数据可能不完整
- `last_state_db_status = error`
  - SQLite 无法连接或查询
  - 常见原因包括锁冲突、损坏或 schema 不兼容

建议恢复步骤：

1. 先确认 `state_db_path` 指向的是否是正确文件
2. 如果本机实际状态库不在默认位置，改用 `--state-db-path`
3. 修正路径后执行一次 `--rescan --backfill-only`
4. 再查看 `archive-audit.md`，确认状态是否恢复为 `ok`

## 输出内容的边界说明

这个脚本面向“本地已落盘的 Codex 会话数据”。它不能保证得到所有运行时上下文，特别是：

- 隐藏思考原文通常不可读
- 某些 UI 层状态不一定出现在本地结构化文件里
- 自动背景压缩是否发生，不作为判断计划是否丢失的依据
- Codex 的本地存储结构未来可能变化；当前脚本对缺失或不兼容的 SQLite 状态库会做降级处理，但一旦上游 schema 变化，线程元数据质量可能下降，直到脚本更新

对于计划保留问题，脚本采用的唯一真相源是 rollout JSONL。

## 相关文档

- 英文 README: [README.md](./README.md)
- 贡献指南: [CONTRIBUTING.md](./CONTRIBUTING.md)
- 发布清单: [PUBLISHING_CHECKLIST.md](./PUBLISHING_CHECKLIST.md)
- 独立仓库忽略规则模板: [.gitignore.example](./.gitignore.example)
- 脚本：[watch_codex_sessions.py](./watch_codex_sessions.py)
- 测试：`tests/test_watch_codex_sessions.py`

## 公开仓库安全提示

这套代码和文档本身适合公开，但真实归档数据通常不适合直接公开。公开前请避免上传：

- `output/codex-archive/`
- 真实会话转录
- 真实计划导出结果
- 从 `~/.codex` 直接复制出来的原始状态数据

如果你把这个目录拆成独立仓库，建议在首次公开推送前，把 [.gitignore.example](./.gitignore.example) 改名为 `.gitignore`。
