# 设计说明：为什么这个工具长这样

README 讲「怎么用」，这里讲「为什么这么做」。这些内容原本挤在 README 里，把「30 秒上手」
压得喘不过气；读的人通常只想知道命令怎么敲，而想改适配器的人需要的是这一份。

## 1. `fetch = collect + derive`

适配器被拆成两半：

- `collect(client, ref, ctx)`：只做请求。每个响应交给 `RawStore` 记进 `raw/`——**文本按收到的
  字节写盘，JSON 按解析结果紧凑重写**（内容等价，不是字节等价）；
- `derive(parts, ctx)`：**纯函数**。只读那些记下来的响应，算出 `Playlist`。

`mpa fetch` 是两者先后跑一遍；`mpa verify` 是把 `raw/` 读回来、**再跑一次 `derive`**，然后与
`playlist.json` **逐字节比对**。这条设计的价值在于它是「能自己抓自己 bug 的不变量」：

- `collect` 里如果混进了「只有当时才知道」的东西（当前时间、随机数、某个中间变量），
  verify 立刻会红——所以这类值必须在 `collect` 阶段写进 `ctx`，由 `fetch.log.json` 持久化；
- `Adapter.fetch` 还会检查「交给 `derive` 的 parts」与「写进 `raw/` 的 parts」**完全一致**，
  不一致当场报错，而不是等三个月后别人复算时才发现。

它管的是「记的等于用的」，管不了反方向的「收到了却没用」——那个方向要靠人把 `raw/` 摊开、
逐字段问「平台给了这个，我的输出里有吗」。两个方向都抓到过真 bug，见第 4 节。

## 2. 六条验收标准是怎么来的

写这个工具时先定死了六条「算不算取到」，代码是为它们服务的，不是反过来：

| 标准 | 落点 | 怎么验证 |
| --- | --- | --- |
| 数量对不上必须说清楚 | `ledger.declared`/`fetched`/`ok`/`unavailable`/`gap` | 差额非 0 时退出码 1；`playlist.json` 的 `ledger` 里直接写差额与原因 |
| 每个字段都有出处 | `Field.source ∈ platform/derived/unavailable` | `csv`/`jsonl` 导出里每行都带 `*_source` 列；`mpa doctor --live` 报告「声明可用但一条都没有」的字段 |
| 原始响应留存、离线可复算 | `raw/` + `raw/_manifest.json` 摘要 | `mpa verify <dir>` 重新 `derive` 并逐字节比对 |
| 数据格式版本化 | `"schema": "mpa/playlist@1"` | `mpa verify` 检查 schema，字段变更必须升版本 |
| 上游变了能发现 | 契约测试 + `mpa doctor --live` + canary 工作流 | 结构变化抛 `UpstreamChanged`，退出码 3（不是「成功取到 0 首」） |
| 不猜 | 取不到就 `{"value": null, "source": "unavailable", "reason": "..."}` | 不存在把 null 说成 0、把旧年份当发行日期的字段 |

其中「不猜」是最容易被违反的一条：平台的社交计数每次都变、字段语义经常要一张映射表才读得懂，
把它们塞进数据里，下游就会当成事实用。所以判别标准是——**平台当次给的值、且不需要我们的解释
就能读懂**，才进数据（详见 [schema.md](./schema.md) 第 11 节）。

## 3. `mpa verify` 到底检查什么

五项检查，前四项任一条不过就退出码 2：

1. `schema_ok`：`playlist.json` 的 schema 是 `mpa/playlist@1`；
2. `manifest_ok`：`raw/` 每个响应重新 sha256 后与 manifest 相符，且 `raw/` 里没有 manifest
   未记录的文件（`raw/` 是整目录换入的，所以多出来的文件只可能是事后被人手工加进去的）；
3. `raw_digest_ok`：`playlist.json` 记的摘要与 manifest 一致；
4. `derive_deterministic`：断网重算的结果与 `playlist.json` **逐字节相同**；
5. `adapter_version_ok`：数据是不是当前这版适配器生成的——**默认只当警告**，`--strict` 才算失败。
   verify 要回答的是「数据变了吗」，不是「工具版本变了吗」。

`adapter_version`、`raw_saved`、`raw_digest`、`requests` 这四项描述的是抓取过程，verify 会先从
`fetch.log.json` 恢复它们再比对，所以一份旧版本抓的数据只要还能离线复算出同样结果，默认仍然通过。

几个边界的处理方式：

- **数据集缺件**（`raw/` 少一个 part、或少了 `_manifest.json`）在复算之前就被拦下，一行中文
  错误加退出码 2，不会退化成一个 `KeyError` 栈；
- **复算时适配器抛 `UpstreamChanged`**（这份记录的字段形态已经不满足当前契约断言）→ 退出码 3，
  这是 verify 唯一不进 0/2/4 的情形；
- **写盘**：响应先写 `<outdir>/raw.incoming/`，全部成功后才整目录换入 `raw/`；两个 JSON 用
  「临时文件 + `os.replace`」。所以抓取失败**不会**污染上一次的数据集，而提交阶段被打断会
  让 verify 响亮失败（而不是留下一个看着完整、其实混了两个版本的目录）。

## 4. 这套不变量抓到的真 bug

**「记的等于用的」方向（2 个）：**

- QQ 的专辑详情请求体（`*_req` part）没有记进 `raw/`：线上能算出语种，离线复算算不出来；
- 酷狗的 `track_all_*_count` 是从响应里数出来的派生值，却被当成「收到的响应」写进了 parts：
  离线复算时凭空多出一份原文件。

**「收到了却没用」方向（2 个）：**

- 汽水 65/65 曲目都有 `lang_codes`，但 `language` 被写死成 `unavailable`、`capabilities` 写成
  `false`——平台给的语种 100% 被丢；
- 同一份响应里 65/65 曲目都有 `Genre` 标签，而整段 `tags` 没解析，曲风一个都没进数据。

四处都已修，修完守着同一条规矩：只透传平台值、不做代码→显示名映射、不归类，`raw` 不动、
`playlist.json` 离线重算、`mpa verify --strict` 五项全过。**列不出「为什么不取」的理由的字段，
就是还没修完的 bug**——这条规矩的产物是 [schema.md](./schema.md) 第 11 节那张表。

## 5. canary 为什么只比形状，不比取值

`.github/workflows/canary.yml` 每天真抓一遍，把结果与入库 fixture 做**结构比较**
（`tools/contract_diff.py`）：只看曲目键、规范字段的 `source` 覆盖率、`extra` 里的平台附加字段
还在不在、`ledger.declared` 和请求计划。

歌单加歌、点赞数变化、汽水分享页每次请求都不同，这些都不该让 CI 变红；而「某个字段整批消失」
「总数从数字变成 null」「少发了一次请求」必须变红——计划是照形状写的，跑起来对不上就说明平台
接口动了。`extra` 的键只判「整批消失」，不判覆盖率比例：汽水的曲风是平台自己会摇的字段，
比例抖动是上游行为，不是回归。

同理，**`raw_digest` 不能用来跨时间比较**：它只是「这一次抓取」的指纹，平台响应里任何无关字段
动一下它就变（汽水实测：同一歌单隔几小时抓两次，65 首的内容逐字节相同，`raw_digest` 却从
`43791ffa…` 变成 `98c08679…`）。判断上游有没有变，要比派生结果或形状。

## 6. canary 第一次在真 runner 上跑（2026-10-12）

四个平台全绿（`drift=0 fail=0`），kugou 的自动 provision（下载上游 tarball + `npm install`）
一次通过。但第一次跑就暴露了三件事，都已修：

1. **产物 73 MB**：日志收集那步把整个 `$RUNNER_TEMP/mpa-home` 收进了产物，而 kugou 的 node 服务
   会把 `node_modules` 装在里面——下载比跑一趟还慢。改成只收 `*.log`（深度 3 以内），产物降到
   2.7 MB（原始响应 + 日志）。
2. **报警路径没被验证过**：跑成功时「失败时开 issue」是 skipped。报警路径只有真的响过一次才算
   验证过，所以加了 `verify_alerting` 开关（手动 dispatch 专用）：置 true 时这一步不碰平台、
   直接置失败，用来确认 issue 与 `canary` 标签会被创建。演练开出来的 issue 已关闭并留了说明。
3. **最重的一步该自己限时**：抓取那步加 `timeout-minutes: 15`，kugou 卡住时在这一步失败，
   而不是耗到 job 级的 30 分钟。

## 7. CI 在真 runner 上抓到的两个问题

本地永远看不见，第一次跑 workflow 就红了：

1. workflow 把 `${{ runner.temp }}` 写在 **job 级** `env` 里，GitHub 直接判
   `Invalid workflow file`（job 级可用的上下文只有 `github`/`needs`/`vars`/`strategy`/`matrix`/
   `inputs`）；改成用 `$RUNNER_TEMP` 写进 `$GITHUB_ENV` 后正常。
2. 有一条测试断言「doctor 的输出里没有 `[skip]`」——本机装了 pnpm 所以从不出现，runner 上没有
   pnpm，`[skip] pnpm` 立刻让测试红。这条断言把「我的机器」当成了契约，已改成「live 段里 kuwo
   那行是 `[  ok]` 而不是 FAIL」，并补了一条用伪造报告锁住 `[skip]` 语义的测试。

教训是一样的：**断言要表达契约，不要表达「我这台机器上恰好是这样」**。

## 8. 文档也会烂，所以让 CI 去查

README 的卖点是「每句话都能用命令验证」，可它自己曾经长期引用着不存在的东西：两个早已删掉的
子命令（`facts`、`analyze`）、一个不存在的语言启发模块、退出码表里的 `diff`，还有八个指向已删
章节的章节号。（这一节刻意不写出那两个命令的完整拼法——守卫没有白名单，白名单最后都会变成垃圾桶。）
现在 `tools/check_doc_refs.py` 每次 CI 都查四件事：子命令必须真实存在（认
`mpa.cli.SUBCOMMANDS`）、反引号里的 `.py` 路径必须存在、markdown 链接必须指向存在的文件、
`第 N 节` 必须能在目标文档里找到（同一个文档里章节号还不许重复）。

## 9. 目录分工

| 文件 | 给谁看 |
| --- | --- |
| `README.md` | 第一次打开仓库的人：怎么装、怎么跑、平台支持到什么程度 |
| `docs/design.md`（本文） | 想改适配器、想知道某条规矩为什么存在的人 |
| `docs/schema.md` | 数据契约本身：字段、`ledger`、退出码、`verify` 的检查、哪些字段「看过但不取」 |
| `docs/coverage.md` | 字段覆盖表（自动生成，CI 拦过期） |
| `docs/references/<平台>.md` | 半年后回来修某个平台适配器的人：请求细节、单位坑、已失效的路线 |
| `docs/sources.md` | 证据分级：哪些说法有一手数据支撑，哪些还没复核 |
| `tests/README.md` | 改测试的人：怎么跑、测什么、fixture 的隐私红线 |
