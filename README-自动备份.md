# dsh-purge 上游镜像与 Telegram 备份

工作流：`.github/workflows/fork-backup.yml`，显示名称 **Upstream mirror and Telegram backup**。

## 工作方式

- `master` 保留可信的备份脚本，不再合并上游，也不由定时任务改写。
- `upstream-mirror` 保存固定上游仓库 `YuJunZhiXue/dsh-purge` 默认分支的原始源码 ZIP 与 `manifest.json` 提交清单；默认分支更名时自动跟随。它是数据快照分支，源码在 ZIP 内，不是直接指向上游提交。
- 普通更新、强推、回退、重写历史、没有共同祖先或上游修改工作流，都不再因合并检查而停止备份。
- 每次源码或上游 refs 变化生成一个快照提交，父提交是上一份快照，旧快照不会丢弃。打包前从上一份 ZIP 的 Bundle 恢复旧对象，再抓取新 refs，重写前的提交和注解标签对象仍留在新的历史包中。
- 快照使用精确租约推送，拒绝并发覆盖；投递标记同样绑定同步时读取的旧值。分支保护或并发变化拒绝时明确失败，下次再试。
- 不执行上游脚本或工作流，也不安装依赖、不构建。源码 ZIP 的发送不依赖上游是否能构建。

GitHub 默认令牌没有修改工作流文件的额外权限，因此只推控制器生成的 ZIP/JSON 数据提交，不把上游 `.github/workflows` 或原始上游提交推成可执行仓库内容。上游工作流仍完整保存在源码 ZIP 和历史包中，无需新增 PAT。历史包排除快照分支本身，避免旧 ZIP 递归嵌入新 ZIP。

这不是所有分支都各发一个 ZIP：Telegram 发送的是**上游当前默认分支**的源码，历史包则包含已获取的各分支、标签与快照。仅其他分支或标签变动时归档对象，不重复发送默认分支的同一个提交。

## Telegram 消息

每次只发送一条富消息，附一个 ZIP：

```text
版本号
名为 dsh-purge 的对应版本；没有有效版本时显示提交短 SHA

更新说明
对应版本的中文更新内容；缺失时明确注明

文件
[dsh-purge-v版本-提交短SHA.zip]
完整源码，含 Git 提交历史；解压后按 GIT-HISTORY.md 恢复分支、标签及提交记录。
```

版本和日志直接从待发送源码读取，缺少、损坏或过大的说明文件不会阻止源码备份。支持版本时只匹配该版本日志，优先中文，不套用其他版本的说明。文件名中的提交 SHA 可以区分同版本的不同提交。

ZIP 包含：

- 上游当前源码，保留 LICENSE、NOTICE 与署名。
- `dsh-purge/history.bundle`：已获取的上游分支、标签与此前保留的历史对象，也保留控制库 master 及旧同步基线的历史；不包含快照 ZIP 分支本身。
- `dsh-purge/GIT-HISTORY.md`：该快照的恢复命令。

Telegram 不另外发送 Bundle、安装包、manifest 或校验清单。源码 ZIP 超过 45 MiB 时明确失败，可从本次 Actions artifact 下载，不拆成多条消息。

## 配置与触发

在仓库 Settings → Secrets and variables → Actions 添加：

| Secret | 说明 |
|---|---|
| `TG_BOT_TOKEN` | BotFather 给出的完整 Token |
| `TG_CHAT_ID` | 目标群/频道 ID，保留负号，或公开频道用户名 |
| `TG_MESSAGE_THREAD_ID` | 群话题正整数 ID；不使用话题时不设置 |

机器人需要向目标聊天和话题发送消息、文件的权限。频道关联讨论群的话题需要填写讨论群自己的 Chat ID。

同步、快照和投递标记使用 GitHub 自动提供的 `GITHUB_TOKEN`，不需要另加 PAT。仓库/组织规则必须允许更新 `upstream-mirror` 与 `backup-delivered`。不能通过代码绕过分支保护规则。

计划为每 5 分钟检查一次；GitHub 调度可能延迟或丢弃运行，不能保证实时。计划只在 fork 的默认分支 `master` 执行，工作流需保持启用。也支持 `repository_dispatch` 的 `upstream-updated` 事件。

手动验证：Actions → **Upstream mirror and Telegram backup** → Run workflow，分支选择 `master`。勾选 `force_backup` 会重复发送当前提交；不勾选则只发送尚未成功投递的当前提交。

成功顺序为 `sync → deliver → mark-delivered`。`backup-delivered` 只在 Telegram 成功后指向实际发送的快照提交，其 `manifest.json` 记录上游 SHA；失败会在后续运行重试。上游回退到与上次投递不同的提交也会发送。无变化时复用已有快照，不新增提交、不再投递。

`backup-upstream` 和 `backup-upstream-before-20261029` 是旧方案留下的记录，不再参与判断，不自动删除。请勿手动修改镜像快照分支或投递标记。

## 恢复与边界

直接解压 ZIP 即可查看源码。需要完整 Git 仓库时，在解压后的 `dsh-purge` 目录按 `GIT-HISTORY.md` 执行；恢复到新的目录，避免覆盖现有文件。fork 的 `master` 是控制库，`upstream-mirror` 是原始源码的 ZIP 快照库。

```sh
git clone --mirror history.bundle restored.git
git --git-dir=restored.git fsck --full
# 后续按 GIT-HISTORY.md 用本次源码 SHA 建立 restored-source 分支，再克隆工作副本。
```

生成的恢复说明另含本次准确提交 SHA 的 checkout 命令。向其他 GitHub 仓库恢复前先检查 refs，不要未经检查向已有仓库执行 mirror 推送。

不包含首次获取之前已经不可达的提交、LFS 实际大文件、子模块仓库、Issues/PR、Release 附件或 Secrets。两次实际检查之间出现又被删除的提交、分支或标签不能保证捕获。上游删除、转私有、身份改变或网络故障时停止并保留同步前 Bundle；同名重建的不同仓库不会自动当成原上游。

Actions artifacts 默认保留 90 天，受仓库存储额度及策略约束；成功推送的快照提交链持续留在 fork，Telegram ZIP 也可另存本地。保留所有已观察历史会让仓库和 ZIP 随时间增长。上传或推送失败时不能承诺持久保存新对象，应及时检查失败运行的 artifacts。

HTTP 429 有限重试；其他 Telegram 错误明确失败。网络超时可能已经发送成功，所以不在同一次请求里自动重发；整次重跑仍可能产生重复消息。

## 本地验证

无需新增 Python 依赖：

```sh
python -B tests/test_backup.py
npm test
```

Python 测试使用临时仓库和模拟网络，不写真实 GitHub 或发送 Telegram 消息。覆盖历史重写、回退、无共同祖先、默认分支更名、控制脚本隔离、历史恢复、并发租约、失败重试、重复抑制及单条源码消息。
