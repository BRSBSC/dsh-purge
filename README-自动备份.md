# dsh-purge 自动同步、构建及 Telegram 备份

工作流位于 **BRSBSC/dsh-purge 的 master 分支**。Telegram 每次只收到一条富消息，按“版本号 → 更新说明 → 文件”排列，附一个源码 ZIP。Bundle、构建安装包及 manifest 仅保留在 GitHub Actions artifacts 中。

## 仓库检查结果

检查日期：2026-10-28（北京时间）。

| 项目 | 实际结果 |
|---|---|
| Fork | `BRSBSC/dsh-purge`，默认分支 `master` |
| 上游 | `YuJunZhiXue/dsh-purge`，默认分支 `master`，GitHub 仓库 ID `1339256252` |
| 检查提交 | `160b9114cbad7beca3d4d3f9f027ec843bf46f5e` |
| 类型/版本 | Node.js ESM 插件，`dsh-purge@1.1.81`，要求 Node >=18 |
| 构建 | `npm run build:client`，更新已存在的 `client.js` |
| 测试 | `npm test` / `node --test` |
| 依赖 | 无 dependencies/devDependencies；仅 peerDependency `@deepseek-ai/cordis`，当前无需 npm install |
| 原发布工作流 | `.github/workflows/release.yml`，入口语法检查、模块导入检查、`git archive` 源码 ZIP；仅在原作者仓库执行 |

构建脚本识别 `package.json`，优先执行 `build`，其次 `build:client`，有 `test` 就运行。安装包使用 `npm pack --ignore-scripts`，保留 LICENSE、NOTICE 与原作者署名。源码 ZIP 是提交原样快照，`.tgz` 才包含本次重建的客户端；后者只保存在 GitHub Actions，不会发到 Telegram 或发布到 npm。

这是针对本仓库实际结构的识别器；项目未来改成其他语言时会明确失败，不会输出伪装成构建产物的源码包。有新依赖时要求 `package-lock.json`，用 `npm ci --ignore-scripts` 安装；需要安装钩子的依赖须人工审查适配。

## 部署

1. 复制文件并提交到 `master`；原有业务文件及发布工作流无需修改。
2. GitHub → Settings → Actions → General：允许 GitHub Actions；Workflow permissions 允许读写仓库。组织策略、分支保护和 ruleset 也必须允许机器人向 `master`、`backup-upstream`、`backup-delivered` 推送；本方案不会绕过保护规则。
3. Settings → Secrets and variables → Actions → New repository secret，添加下表配置。
4. 在 fork 的 Actions 页面启用工作流，手动运行 **Fork sync, build and Telegram backup**。第一次运行即备份；勾选 `force_backup` 可重复构建与上传。
5. 检查四个 job 成功、Telegram 中收到一条附源码 ZIP 的消息。以后仅在提交尚未上传成功时构建上传；没有变化时仍检查上游，但不保留重复 Actions artifacts。

| Secret | 必填 | 说明 |
|---|---|---|
| `TG_BOT_TOKEN` | 是 | BotFather 创建机器人后给出的完整 token |
| `TG_CHAT_ID` | 是 | 群/超级群/频道 ID，通常为 `-100...`；公开频道也可用 `@channelname` |
| `TG_MESSAGE_THREAD_ID` | 群话题必填 | 超级群目标话题的正整数 ID；普通群和频道直接上传时留空 |

不需要 PAT：同步和记录进度使用 GitHub 自动提供的 `GITHUB_TOKEN`。不要将 token 写入仓库或终端共享日志。

### Telegram 配置

通过 Telegram 的 `@BotFather` 使用 `/newbot` 创建机器人。将机器人添加到目标群并允许发送消息/文件；频道则需将机器人设为管理员并授予发帖权限。

**群话题属于启用了 Topics 的超级群。频道本身没有这种群话题**：如要上传到频道关联讨论群的话题，`TG_CHAT_ID` 必须填讨论群 ID，thread ID 也必须来自同一个讨论群。

获取准确 ID：在目标话题发送 `/backup@你的机器人用户名`，在本地调用 Bot API `getUpdates`，读取同条 `message` 的 `chat.id` 与 `message_thread_id`。普通群可能没有 `message_thread_id`；频道更新在 `channel_post.chat.id`。如果机器人已使用 webhook，`getUpdates` 不能同时消费，请从既有 webhook 的更新中读取，避免擅自删除 webhook。不要将包含 bot token 的请求地址发给别人。

使用 Telegram Bot API `sendRichMessage`，将标题、更新说明和文档块合成一条消息：

```text
版本号
dsh-purge v1.1.81

更新说明
• 对应版本的中文更新内容……

文件
[dsh-purge-v1.1.81-提交短SHA.zip]
完整源码快照，解压后查看；不含 Git 提交历史。
```

版本号和更新说明直接读取待发送的 `source.zip` 中的 `package.json` 和 `release-notes.md`，只匹配该版本，并优先选中文部分；未匹配时标明“该版本未提供更新说明”，不会套用其他版本日志。Telegram 不再发送 Bundle、`.tgz`、manifest、重复 ZIP 或独立校验清单。源码文件名包含版本号和提交短 SHA，同版本的不同提交也能区分。需要支持富消息的 Telegram 客户端才能完整呈现分段布局。

上传脚本对 HTTP 状态和 JSON `ok` 都做检查，429 按服务端等待时间有限重试；其他失败退出。源码 ZIP 超过 45 MiB 时明确失败，请改从 Actions 下载，不拆分为多条消息。网络超时不自动重发，避免服务端已接收时重复发送；整次手动或定时重跑仍可能重复。

## 触发与实时性的边界

默认 `3-58/5 * * * *`：每小时 UTC 第 3、8、13…58 分钟执行（北京时间同分钟）。GitHub 最短计划间隔为 5 分钟，繁忙时可能延迟或丢弃排队任务，**不能保证上游一更新就立即构建**。上游的 push 不会自动触发 fork。

如果可以让上游维护者配置 webhook/Action，或已有外部监控器，可调用 GitHub `POST /repos/BRSBSC/dsh-purge/dispatches`，JSON 为：

```json
{"event_type":"upstream-updated"}
```

认证账户必须有目标仓库相应写权限（fine-grained token 的 Contents: write）；该凭据存于外部触发端，不要存入上游公开代码。上游 push webhook 的原始载荷不能直接当成 dispatch API 请求，需要中转并校验 webhook 签名。workflow 不信任传入的 SHA/分支，始终重新读取固定上游 master。没有外部触发端时，轮询足够工作。

公开仓库连续 60 天无活动时计划工作流可能自动停用，fork 初始也可能需要手动启用。若需要长期无人值守，请使用独立外部定时器并监控 Actions 是否仍启用，不能把“轮询一直运行”当作保证。

## 数据流与保护

1. 可信脚本取自本次触发的提交；另一个 checkout 获取最新 `master` 和 fork 所有分支/标签的完整可达历史。
2. **联网访问上游前**，创建并验证 `before-sync.bundle`。上游 404、403、网络故障、私有化时，job 失败，仍尽力上传这个 artifact；fork master 保持原状。故障时不执行构建和 Telegram 上传，可从失败运行下载 Bundle。
3. 校验上游固定仓库 ID 和默认分支。删除后同名重建的不同 ID 不会被当成原仓库；首次部署前发生且无任何已保存证据的历史变化不能追溯判断。
4. 读取上游所有分支、标签到独立 refs，不删除 fork 的标签/分支，不使用 `reset --hard`、强推或远程 prune。利用 `backup-upstream` 保存最后同步的上游 master；一旦该提交不再是新上游的祖先，停止，防止强推/回退悄悄替换历史。
5. 上游改变 `.github/workflows/` 或 `scripts/fork-backup/` 时暂停，要求人工审查，以免上游修改控制流程、接触备份密钥。即使是正常 release 工作流更新也会暂停。审查后在本地手动合并、处理冲突，并由有 workflow 写权限的维护者提交，再运行本工作流。不要通过删除检查来自动接受。
6. 正常 git merge 保留 fork 自己的提交；冲突则 abort，远程 master 不变。成功后生成 `history.bundle`、`source.zip` 和 manifest，再**原子、非强制**推送 master 和 `backup-upstream`；任一规则拒绝、远程并发变化都让同步失败。
7. 无写权限、无 TG Secrets 的独立 runner 构建测试。独立上传 runner 只把 artifacts 当数据读取，持有 TG Secrets；不会执行构建产物。此隔离防止普通上游构建脚本直接获得 TG token，但不代表已审查上游业务代码安全。
8. 源码 ZIP 富消息发送成功后，才把 `backup-delivered` 推进至本次 fork 提交。构建/上传失败，下次轮询会重试；上游没有新提交也不影响重试。后续 master 合并了新内容时，标记仍只指向实际上传的提交。

`backup-upstream`、`backup-delivered` 是自动化保留分支，请勿手工修改或删除。仓库层面的 concurrency 避免本工作流之间相互覆盖；人工及其他工作流同时推送造成的非快进会安全失败。

同步提交在构建前进入 master；构建失败时不会回滚同步，已有同步前后 Bundle 可下载，后续继续重试。这保证同步失败停止后续流程，但不承诺把“同步和构建”做成跨系统事务。

### Bundle 的完整范围

`git bundle create --all` 配合非浅克隆，保存**当前可获取的 fork 与上游全部分支和标签可达的 Git 对象和提交历史**。上游标签存入 `refs/upstream-tags/*`，避免覆盖 fork 同名标签；上游分支在 `refs/remotes/upstream/*`。每个成功变更运行会产生独立、可恢复的完整 Bundle，不依赖增量链。

它不包含上游在首次备份前已删除、又不可达的提交；不含 GitHub Issues、PR、Release 附件、LFS 实际大文件、子模块仓库、GitHub Secrets、未提交文件或服务器 reflog。当前交付没有为这些另加备份。单次轮询之间出现又删除的分支，也无法保证捕获。默认只跟踪 master 的变化来决定构建；仅修改其他分支/标签不会触发新上传，可手动 force_backup 获取快照。

Actions artifacts 默认保留 90 天（还受仓库/组织上限及存储额度约束），不是永久备份。需要历史恢复时，定期下载 GitHub Actions 中的 Bundle 到本地或独立存储。上游不可用时请及时下载失败运行的 `before-sync.bundle`。Telegram 中的源码 ZIP 不含 Git 历史。

## 恢复

直接解压 Telegram 中的 ZIP 即可查看源码。要恢复分支、标签及提交历史，请从 Actions 的 `history-运行ID-尝试次数` artifact 下载并解压 `history.bundle`：

```bash
git clone --mirror history.bundle restored.git
git --git-dir=restored.git fsck --full
git clone restored.git restored-worktree
```

manifest 记录已同步提交；必要时在恢复的工作副本中 checkout 该 SHA。若 bare 仓库 HEAD 未指向 master，可执行 `git --git-dir=restored.git symbolic-ref HEAD refs/heads/master` 再克隆。

向新 GitHub 仓库恢复时，先检查 `git --git-dir=restored.git show-ref`，明确选择所需 branches/tags 推送。Bundle 含自动化、上游远程及标签命名空间，**不要未经检查直接 mirror 推送到已有仓库**。

## 本地验证

Python 3.10+、Git、Node.js 22 与 npm 即可；脚本没有新增 Python 依赖。

```bash
python -m unittest discover -s tests -v
# 在项目根目录执行，输出放在仓库外：
python scripts/fork-backup/build.py ../verified-build
```

自检不发送 Telegram 消息，不修改真实 GitHub 分支。覆盖历史改写、控制文件修改、冲突、上游不可用、Bundle 恢复、源码 ZIP 单消息选择、版本日志匹配、富消息话题载荷、超限拒绝与本地原子推送/完成标记判断。

## 官方行为依据

- [GitHub Actions 触发事件、schedule、repository_dispatch](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)
- [工作流自动停用及重新启用](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows)
- [Telegram Bot API：sendRichMessage](https://core.telegram.org/bots/api#sendrichmessage)

首次部署已完成 GitHub runner 与真实 Telegram 投递验证；更改发送格式后应再手动运行一次确认客户端显示效果。
