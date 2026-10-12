# playlist-dig（命令行工具 `mpa`）

把一个音乐平台的歌单，取成**一份能被人反复核算的数据**：每个字段都说得出是从哪个响应里来的，数量对不上会明说，原始响应全部留档（文本原样落盘，JSON 按解析结果紧凑重写，内容等价），断网也能重新推导一遍。

它产出的是一个**数据集目录**（`playlist.json` + `fetch.log.json` + `raw/`）和几种数据格式的导出；取数链路里不做任何猜测——拿不到就写 `unavailable` 并说明原因，绝不填一个看起来合理的值。想拿它做统计、报告还是图表，是下游自己的事。

更早那版（各平台的抓取脚本 + 一个检查脚本）问题意识是对的——把歌单取全、说清哪些取不到——但实现上有几处硬伤：文档与脚本混在一起、正文里夹着大段 prompt 文本、零测试、五个平台共用一堆复制粘贴的 `requests` 调用、`实测` 数字无法复核、README 的安装步骤照做会失败。这一版保留问题意识，把「可复核」做成结构：`fetch = collect + derive` 的拆分、四份真实抓取记录入库、476 条测试、版本化契约 `mpa/playlist@1`。

## 30 秒上手

```bash
cd playlist-dig

# 1. 抓一条歌单（网易云/QQ/酷我给链接就行，酷狗/汽水有额外的本地服务说明）
python3 -m mpa fetch "https://y.qq.com/n/ryqq/playlist/7011264340" --out ./out

# 2. 断网复算：raw/ 里的原始响应能不能推出同一份 playlist.json
python3 -m mpa verify ./out

# 3. 导出成别的格式：json / jsonl / csv / txt
python3 -m mpa export ./out --format csv --out tracks.csv
```

`fetch` 的输出目录就是交付物本身，可以直接丢进 git、发给别人、或者当作回归测试的输入：

```
out/
  playlist.json     # 唯一的事实来源：mpa/playlist@1
  fetch.log.json    # 这次抓取的元信息（模式、上游 commit、请求数、警告、退出码）
  raw/              # 每个请求的原始响应 + _manifest.json（含每一项的 sha256 与总摘要）
```

## 六条验收标准

写这个工具时先定死了六条「算不算取到」，代码是为它们服务的，不是反过来。

| 标准 | 落点 | 怎么验证 |
| --- | --- | --- |
| 数量对不上必须说清楚 | `ledger.declared`/`fetched`/`ok`/`unavailable`/`gap` | 差额非 0 时退出码 1；`playlist.json` 的 `ledger` 里直接写差额与原因 |
| 每个字段都有出处 | `Field.source ∈ platform/derived/unavailable` | `csv`/`jsonl` 导出里每行都带 `*_source` 列；`mpa doctor --live` 报告「声明可用但一条都没有」的字段 |
| 原始响应留存、离线可复算 | `raw/` + `raw/_manifest.json` 摘要 | `mpa verify <dir>` 重新 `derive` 并逐字节比对 |
| 数据格式版本化 | `"schema": "mpa/playlist@1"` | `mpa verify` 检查 schema，字段变更必须升版本 |
| 上游变了能发现 | 契约测试 + `mpa doctor --live` + canary 工作流 | 结构变化抛 `UpstreamChanged`，退出码 3（不是「成功取到 0 首」） |
| 不猜 | 取不到就 `{"value": null, "source": "unavailable", "reason": "..."}` | 报告里不存在把 null 说成 0、把旧年份当发行日期的字段 |

第三条是这一版的核心机制，值得单独说：适配器被拆成

- `collect(client, ref, ctx)`：只做请求，把每个响应记进 `raw/`（文本按收到的字节写盘，JSON 按解析结果紧凑重写）；
- `derive(parts, ctx)`：**纯函数**，只读那些记下来的响应，算出 `Playlist`。

`fetch = collect + derive`，而 `mpa verify` 是把 `raw/` 读回来再跑一次 `derive`，和 `playlist.json` 做逐字节比对。`collect` 里如果混进了「只有当时才知道」的东西（当前时间、随机数、某个中间变量），verify 立刻就会失败——所以适配器必须在 `collect` 阶段把这些写进 `ctx`，由 `fetch.log.json` 持久化。另外，`Adapter.fetch` 会检查「交给 `derive` 的 parts」和「`raw/` 里记下的 parts」完全一致，不一致就当场报错，而不是等到三个月后别人复算时才发现。这条不变量在本项目里已经抓出两个真 bug；它管的是「记的等于用的」，管不了反方向的「收到了却没用」，那个方向也抓出过两处（详见「已知缺口」）。

`mpa verify` 会打印五项检查，前四项任一条不过就退出码 2：schema 是 `mpa/playlist@1`；`raw/` 每个响应重新 sha256 后与 manifest 相符、且 `raw/` 里没有 manifest 未记录的文件（两代适配器混在同一个目录里，就没人说得清 `playlist.json` 是从哪来的）；`playlist.json` 记的摘要与 manifest 一致；断网重算的结果与 `playlist.json` 逐字节相同。第五项 `adapter_version_ok`（数据是不是当前这版适配器生成的）默认只当警告——verify 要回答的是「数据变了吗」，不是「工具版本变了吗」——加上 `--strict` 才让它算失败。`adapter_version`、`raw_saved`、`raw_digest`、`requests` 这四项描述的是抓取过程，verify 会先从 `fetch.log.json` 恢复它们再比对，所以一份旧版本抓的数据只要还能离线复算出同样结果，默认仍然通过。往同一个目录里二次抓取时，上一次留下的响应会被清掉，名单记在 `fetch.log.json` 的 `raw_cleanup` 里。数据集缺件（`raw/` 少一个 part、或少了 `_manifest.json`）在复算之前就会被拦下，一行中文错误加退出码 2，不会退化成一个 `KeyError` 栈；而如果复算时适配器主动抛出 `UpstreamChanged`（这份记录的字段形态已经不满足当前契约断言），`mpa verify` 会以退出码 3 结束——这是它唯一不进 0/2/4 的情形。

## 覆盖情况（自动生成）

数字来自仓库里的真实抓取记录，改代码忘了改 README 会被 CI 拦下（`tools/gen_readme_numbers.py --check`）。

<!-- coverage:begin -->
<!-- 由 tools/gen_readme_numbers.py 生成，请勿手改；CI 会检查是否过期 -->

| 字段 | `netease` | `qq` | `kugou` | `qishui` |
| --- | --- | --- | --- | --- |
| `duration_ms` 时长 | 200/200 | 76/76 | 50/50 | 65/65 |
| `language` 语种 | 0/200 | 76/76 | 50/50 | 65/65 |
| `released_at` 发行日期 | 200/200 | 76/76 | 50/50 | 65/65 |
| `collected_at` 收藏时间 | 0/200 | 0/76 | 50/50 | 0/65 |

| 声明 / 实取 | 200 / 200 | 76 / 76 | 50 / 50 | 65 / 65 |
| --- | --- | --- | --- | --- |
| 可用 / 不可用 | 200 / 0 | 76 / 0 | 50 / 0 | 65 / 0 |
| 抓取方式 | `direct-http` | `direct-http` | `node-service-external` | `ssr-partial` |

平台专属字段（`Track.extra`，分母是该平台实取到的曲目数）：

| `extra` 键 | `netease` | `qq` | `kugou` | `qishui` |
| --- | --- | --- | --- | --- |
| `album_type` | — | 76/76 | — | — |
| `alias` | 39/200 | — | — | — |
| `bpm` | — | — | 50/50 | — |
| `composers` | — | — | — | 63/65 |
| `fileid` | — | — | 50/50 | — |
| `genre` | — | — | — | 65/65 |
| `label` | — | 75/76 | — | — |
| `lyricists` | — | — | — | 63/65 |
| `popularity` | 200/200 | — | — | — |
| `remark` | — | — | 50/50 | — |
| `subgenre` | — | — | — | 56/65 |
| `trans_name` | 17/200 | — | — | — |

歌单级字段（`playlist.extra`，描述的是歌单本身，不是每首歌）：

| `playlist.extra` 键 | `netease` | `qq` | `kugou` | `qishui` |
| --- | --- | --- | --- | --- |
| `created_at` | 2013-09-09T10:10:06Z | 2019-05-28T01:45:35Z | 2026-01-29T15:14:20Z | 2024-08-02T04:02:53Z |
| `published_at` | — | — | 2026-01-29 | — |
| `tags` | — | — | 流行、网络、甜蜜、精选 | — |
| `track_updated_at` | 2026-09-29T12:32:09Z | — | — | — |
| `updated_at` | 2026-09-29T00:22:25Z | — | 2026-02-13T12:19:44Z | 2026-09-29T15:44:42Z |

上面每一列都对应仓库里的一份真实抓取记录：`tests/fixtures/<平台>/`，里面同时存了 `raw/`（原始响应）、`playlist.json`（推导结果）和 `provenance.json`（出处）。任何人都可以用 `mpa verify tests/fixtures/<平台>` 在断网环境下重新算一遍，对不上就会失败。
<!-- coverage:end -->

## 平台一览

| 平台 | `--provider` | 抓取方式 | 需要 node 服务 | 最后实测 |
| --- | --- | --- | --- | --- |
| 网易云音乐 | `netease` | `direct-http` | 否 | 2026-09-29，热歌榜 `3778678`，200/200 |
| QQ 音乐 | `qq` | `direct-http` | 否 | 2026-09-29，公开歌单 `7011264340`，76/76 |
| 酷我音乐（波点） | `kuwo` | `direct-http` | 否 | **未实测**：手头没有公开的波点歌单 id |
| 酷狗音乐 | `kugou` | `node-service` | 是 | 2026-09-29，`collection_3_2132041455_14_0`，50/50 |
| 汽水音乐（抖音） | `qishui` | `ssr-partial` → `node-service` | 超过 175 首时 | 2026-09-29，分享页 `playlist_id=7397692920558452788`，65/65（未用 node） |

`mpa adapters` 会打印每个适配器的字段能力和最后实测记录；`kuwo` 那行会明确写「未实测」，而不是假装验证过。每个平台的请求细节、单位坑、已经失效的路线，都记在 `docs/references/<平台>.md` 里——那是给半年后回来修适配器的人看的。

## 命令

| 命令 | 作用 |
| --- | --- |
| `mpa fetch <链接或id> --out DIR` | 抓取并写成数据集目录（`--no-raw` 关掉原始留存，不建议） |
| `mpa verify DIR [--strict]` | 离线复算并比对；`--strict` 连适配器版本不同也算失败 |
| `mpa export DIR --format json\|jsonl\|csv\|txt` | 导出成数据格式，默认 stdout；`--out FILE` 写文件，`--out -` 与省略一样是 stdout；`--limit` 只截 txt 列表（0 表示全部） |
| `mpa doctor [--live] [--provider X] [--id ID]` | 自检环境与连通性；`--live` 真发请求验证字段 |
| `mpa adapters` | 列出适配器能力与最后实测时间 |

退出码是契约的一部分，方便直接塞进 CI：

| 码 | 含义 |
| --- | --- |
| 0 | 全部取到，数量对得上 |
| 1 | 取到了，但有缺口（差额、不可用曲目、警告） |
| 2 | 抓取失败：网络、被平台拦截、响应解析不了 |
| 3 | 上游结构变了，需要改适配器：抓取时契约检查不过，或 `verify` 复算时 `derive` 报出这份记录的字段形态已经不认识了 |
| 4 | 输入或参数不对：只给了一串数字而四个平台（网易云/QQ/酷我/汽水）都可能用它、链接没有适配器认识、把 `verify` 指到一个没有 `raw/` 的目录、`mpa` 或子命令的参数写错（argparse 的用法错误也归这一档，不是 2） |
| 5 | 这条路线当前不可用（分享页失效、本地服务起不来） |

库代码里没有 `sys.exit`（`--help`/`--version` 由 argparse 自己以 0 退出），只有 `mpa/cli.py` 把异常映射成退出码，所以 `mpa` 也能当库用：`fetch_playlist` / `verify_playlist` 只抛异常、只返回结果。`mpa doctor` 复用同一张表：环境检查有任何一项 FAIL（例如缓存目录不可写）或 `--live` 的字段验证没过，都是 2；「没有确认可用的样例 id」这种跳过不算失败。

## 目录结构

```
mpa/
  model.py          # mpa/playlist@1 的数据结构（Field/Track/Ledger/Playlist）
  errors.py         # 错误分类与退出码
  runtime.py        # HTTP 客户端：重试、限速、错误映射、顺手记录原始响应
  rawstore.py       # raw/ 的读写与摘要
  pipeline.py       # fetch = collect + derive；verify 的实现
  export.py         # json/jsonl/csv/txt（只有数据格式）
  coverage.py       # 字段覆盖统计（doctor 与 README 覆盖表共用一套口径）
  adapters/         # 每个平台一个文件；base.py 定义 collect/derive 契约
  doctor.py         # 自检
  cli.py            # 命令行入口
tests/
  fixtures/<平台>/  # 真实抓取记录（raw/ + playlist.json + provenance.json）
  README.md         # 怎么加一份 fixture、测试分哪几类、fixture 的隐私红线
tools/
  gen_readme_numbers.py   # 从 fixtures 生成上面的覆盖表（--check 供 CI 用）
  make_provenance.py      # 从抓取输出生成 fixtures/<平台>/provenance.json
  rederive_fixture.py     # 只改了 derive 的解释逻辑时，从磁盘 raw/ 重算 fixture（raw 必须没变）
  contract_diff.py        # 比较新抓取与 fixture 的「形状」：字段、覆盖率、总数、请求计划
docs/
  schema.md         # mpa/playlist@1 契约、字段可得性矩阵、退出码
  references/       # 每个平台的接口笔记与失效症状
.github/workflows/
  ci.yml            # 离线：单测 + fixtures 复算 + 出处复核 + README 数字是否过期
  canary.yml        # 定时：真发请求，发现「上游变了」就开 issue（会自己拉起 node 服务）
SKILL.md            # 给 AI 助手用的使用说明（规则、坑、怎么扩展）
pyproject.toml      # 零依赖；安装后得到 mpa 命令
```

## 开发与测试

```bash
cd playlist-dig
python3 -m unittest discover -s tests      # 全部测试，离线，不联网
python3 -m mpa verify tests/fixtures/qq    # 单看一份 fixture 能不能复算
python3 tools/gen_readme_numbers.py       # 改了适配器之后刷新 README 的覆盖表
python3 tools/check_doc_refs.py           # 文档里提到的命令/文件/章节号是否都还在（CI 也跑）
python3 tools/rederive_fixture.py tests/fixtures/*   # 改了 derive 之后重算 fixture 并顺手 verify
python3 tools/contract_diff.py tests/fixtures/qq /tmp/刚抓的   # 上游形状变了没有
python3 -m mpa export tests/fixtures/qq --format csv  # 看一眼导出的表长什么样
```

测试分两类：fixtures 驱动的是「真实响应能不能稳定推出同一份数据」，单元测试覆盖模型、错误码、raw 存储、重试策略、导出格式。所有测试都不发网络请求；需要真实网络的只有 `mpa doctor --live` 和 `.github/workflows/canary.yml`。

canary 除了真抓一遍，还会把这次的结果和入库 fixture 做**结构比较**（`tools/contract_diff.py`）：只看曲目键、规范字段的 `source` 覆盖率、`extra` 里的平台附加字段还在不在、`ledger.declared` 和请求计划，不看具体值——歌单加歌、点赞数变化、汽水分享页每次请求都不同，都不该让 CI 变红；而「某个字段整批消失」「总数从数字变成 null」「少发了一次请求」必须变红。计划是照形状写的，跑起来对不上就说明平台接口动了。`extra` 的键只判「整批消失」，不判覆盖率比例：汽水的曲风是平台自己会摇的字段，比例抖动是上游行为，不是回归。

## 已知缺口

写清楚哪里还不行，比多写一个功能重要：

- **kuwo 整条链路从未跑通。** 接口路径和必需请求头是从官方 H5 bundle 里反查出来的（`service/playlist/info/<id>` 和 `service/playlist/<id>/musicList`），但没有一个公开可用的波点歌单 id 可供实测。`mpa adapters`、`mpa doctor` 和 `docs/references/kuwo.md` 都标着「未实测」。拿到 id 后请先跑 `mpa doctor -p kuwo --live --id <id>`。
- **汽水分享页硬上限 175 首。** 分享页的 SSR 数据只内嵌前 175 条，翻页参数一律被忽略、滚动也没有 XHR；超过就必须起本地 `qishui-api` 服务。适配器会用「声明数 vs 实取数」发现这一点并把退出码设成 1，而不是把 175 当成全部——静默截断正是最危险的失败方式。
- **网易云没有官方语种字段。** 本工具不猜，所以那 200 条就是 `unavailable` + 原因；按语种归类是下游的事。热歌榜的 `popularity` 是 200/200 的 `100.0`——这份值在曲目之间没有区分度，别拿它当热度排名。
- **网易云的发行日期有一条回退链。** 取 `album.publishTime`，取不到再退到 `song.publishTime`，两边都没有才是 `unavailable` 带原因；`Field.raw` 保留原始时间戳，`value` 统一成 `YYYY-MM-DD`。（仓库里这份 fixture 的 200 首两条路径都有值，回退链本身没被触发过。）
- **QQ 的语种是专辑级的**，同一张专辑的曲目语种相同（这是上游字段的粒度，不是实现取巧），所以它标 `derived` 时会带上 `method`。
- **酷狗的 `gcid_*` 分享链接现在能自动换算，但要走一条有验证的探测。** 分享页仍然内嵌 `window.$output`（歌单名 + 平台声明数 + 前 10 首），但 `global_collection_id` 对用户自己的歌单常常是空串（`no_show_gid=1`）。这时工具会在 `collection_3_<uid>_<idx>_0` 的 idx 1..5 里逐个试，**只有「歌单名相同 且 `list_create_userid` 等于链接里的 uid」才算命中**——这不是猜 id，而是有界搜索 + 身份校验（`idx` 是歌单在该用户列表里的位置）。命中后照常走 node 服务取全量；探测本身需要能起本地服务，起不来或 5 个候选都没命中时，报错会写清「试过哪些、为什么不算数」，并提示改用带明文 id 的链接。
- **汽水分享页每次请求都不一样，所以 `raw_digest` 只能当次快照。** 同一首歌、同一顺序的 65 首，两次抓取的 SSR 页面在 `node_uuid`、头像 CDN 域名轮换、点赞/分享/评论计数、甚至单曲的 `relation_media`（《后来》一次是「电影《后来的我们》片尾曲」、一次是「电视剧《真情》」）上都会变。因此：`raw/` 是某一次的记录，`mpa verify` 只证明**这份** raw 能复算出这份 `playlist.json`；判断上游有没有变，要比派生结果或形状（`tools/contract_diff.py`），不能比 raw 摘要。适配器只取稳定的字段，所以两次抓取的派生结果**除曲风外**逐字节相同——`extra.genre`/`extra.subgenre` 是例外：同一首歌的标签两次抓取能从 `Hip Hop/Rap` 摇成 `Pop`、二级标签摇成空，它记的是「抓的时候平台写的是什么」，不是歌的固有属性（见 [docs/references/qishui.md](docs/references/qishui.md) 的「已知不稳定字段」）。
- **裸数字 id 是歧义的**：网易云、QQ、酷我、汽水都用纯数字 id，所以在没有 `--provider` 时工具拒绝猜，退出码 4。
- **抓取中途失败会留下「半份」`raw/`**：响应文件是边抓边写的，所以失败后 `raw/` 里可能有 manifest 未记录的响应，此时 `mpa verify` 会明确失败而不是假装数据完好。往同一个目录重跑一次 `mpa fetch` 即可自愈（成功结束时会把上一次留下的响应删掉并记进 `fetch.log.json` 的 `raw_cleanup`）。要做到「失败也不污染旧数据」需要先写临时目录再原子替换，这一版没做。
- **`ci.yml` 已经在真 runner 上跑过了**（python 3.10 与 3.13 两档全绿，约 13–18 秒），而且第一次跑就抓到两个本地永远看不见的问题：
  1. workflow 把 `${{ runner.temp }}` 写在 **job 级** `env` 里，GitHub 直接判 `Invalid workflow file`（job 级可用的上下文只有 `github`/`needs`/`vars`/`strategy`/`matrix`/`inputs`）；改成用 `$RUNNER_TEMP` 写进 `$GITHUB_ENV` 后正常。
  2. 有一条测试断言「doctor 的输出里没有 `[skip]`」——本机装了 pnpm 所以从不出现，runner 上没有 pnpm，`[skip] pnpm` 立刻让测试红。这条断言把「我的机器」当成了契约，已改成「live 段里 kuwo 那行是 `[  ok]` 而不是 FAIL」，并补了一条用伪造报告锁住 `[skip]` 语义的测试。
  **`canary.yml` 仍未在真 runner 上跑过**（它只在定时任务与手动 dispatch 时触发）：里面 kugou 走自动 provision（下载上游 tarball + `npm install`），是本项目最重、最容易出问题的一步；`upload-artifact` 与 `github-script` 开 issue 这两条路径也还没被真跑过。
- **开发过程中被「记进 `raw/` 的东西必须等于交给 `derive` 的东西」这条不变量抓到的两个真 bug**：QQ 的专辑详情请求体（`*_req` part）没有记进 `raw/`，后果是线上能算出语种、离线复算算不出来；酷狗的 `track_all_*_count` 是从响应里数出来的派生值，却被当成「收到的响应」写进了 parts，于是离线复算凭空多出一份原文件。两处都已修，`mpa verify` 现在对四个已实测平台全绿——**全绿是指五项检查都 `ok`，不是「主要几项还行」**。
- **反方向也抓到过两个真 bug：数据明明收到了，却被扔掉。** 这条不变量管「记的等于用的」，管不了「收到的没用上」，所以另外做了一遍逐字段对照（把 fixture 的 `raw/` 摊开，一条条问「平台给了这个，输出里有吗」）：汽水 65/65 曲目都有 `lang_codes`，但 `language` 被写死成 `unavailable`、`capabilities` 写成 `false`，等于平台给的语种 100% 被丢；同样 65/65 曲目都有 `Genre` 标签，而整段 `tags` 没解析，曲风一个都没进数据。两处都已修（现在 `language` 65/65，`extra.genre` 65/65、`extra.subgenre` 56/65），修完仍守着老规矩：只透传平台值、不做代码→显示名映射、不归类，`raw` 不动、`playlist.json` 离线重算、`mpa verify --strict` 五项全过。剩下的「看过但不取」的字段连同理由列在 [docs/schema.md](docs/schema.md) 第 11 节——**列不出理由的，就是还没修完的 bug**。

## 许可与致谢

上游接口知识来自各平台自己的 H5 页面与社区服务（`KuGouMusicApi`、`qishui-api`），这些项目的许可与风险由其自身承担；本工具只做只读抓取，不下载音频、不做解密。
