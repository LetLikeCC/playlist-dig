---
name: playlist-dig
description: 用命令行工具 mpa 把音乐平台（网易云/QQ/酷我/酷狗/汽水）的歌单取成一份可离线复算、每个字段都有出处的 JSON 数据集，并可导出 JSONL/CSV/TXT。当用户要求「抓/备份/导出某个歌单」「弄清某个平台的歌单接口」「检查歌单数据缺了什么、哪条路线失效了」时使用。工具位于 $(pwd)/playlist-dig（或用户指定的同名目录），零第三方依赖，只用 Python 标准库。
---

# playlist-dig（`mpa`）

这个工具只做一件事：把歌单取成**能被人反复核算的数据**。它的核心机制是 `fetch = collect + derive`：`collect` 只发请求并把响应记进 `raw/`（文本按收到的字节，JSON 按解析结果紧凑重写——内容等价，不是字节等价），`derive` 是纯函数、只读这些记录，所以 `mpa verify <目录>` 能在断网环境下重算一遍并逐字节比对。

## 先决条件

- `python3` ≥ 3.10，**不需要安装任何第三方包**。
- 只有酷狗（以及超过 175 首的汽水）需要 `node`/`npm` 来起本地服务；工具会自己下载并固定 commit。
- 缓存在 `~/.cache/playlist-dig`；沙箱/CI 里如果不可写，设 `PLAYLIST_DIG_HOME=/tmp/mpa-home`（`mpa doctor` 会直接告诉你目录不可写）。

```bash
cd playlist-dig && python3 -m mpa doctor        # 先看环境，别急着抓
```

## 常用命令

```bash
# 抓一条（链接或 id 都行；纯数字 id 有歧义，见「坑」）
python3 -m mpa fetch "https://y.qq.com/n/ryqq/playlist/7011264340" --out ./out

# 一定要跑这一步：断网复算，证明每个字段都能从 raw/ 推出来
python3 -m mpa verify ./out

# 导出（默认 stdout）：json | jsonl | csv | txt —— 只有数据格式
# csv 的列是固定的，平台专属字段压在一列 extra 里：bpm=95;fileid=847;remark=…
python3 -m mpa export ./out --format csv -o tracks.csv
python3 -m mpa export ./out --format txt --limit 20      # 给人看的清单，0 表示全部

# 自检：--live 会真发请求，用公开样例 id 验证字段是否还取得到
python3 -m mpa doctor --live
python3 -m mpa doctor -p netease --live --id 3778678
python3 -m mpa adapters        # 列能力、上游 commit、最后实测时间
```

退出码是给脚本用的：`0` 全取到；`1` 有缺口（**必须转述给用户，不要当成成功**）；`2` 抓取失败；`3` 上游结构变了（要改适配器，不是重试）；`4` 输入/参数问题；`5` 这条路线当前不可用。

## 使用规则（重要）

1. **交给用户任何数字之前，先跑 `mpa verify`。** 输出目录里 `playlist.json` 是唯一事实来源，报告和 CSV 都是从它派生的。
2. **不要替平台猜字段。** 取不到就是 `{"value": null, "source": "unavailable", "reason": "..."}`。用户问「为什么这里空着」，答案在 `reason` 和 `ledger` 里，不要去别的地方补一个值。
3. **裸数字 id 会被拒绝**（网易云/QQ/酷我/汽水的 id 都是纯数字）：这时加 `--provider`，或让用户给带域名的链接。
4. **缺口必须说清**：看 `ledger.declared / fetched / ok / unavailable / gap`，以及 `fetch.log.json` 的 `warnings`。退出码 1 时，差额就写在 `playlist.json` 的 `ledger.gap` 里（而不是靠人去算）。**`ledger.declared` 为 `null` 也是缺口**（平台没给总数 ⇒ 无法证明完整），同样退出码 1，`gap` 也是 `null`。
5. **`--partial-ok` 不等于「忽略缺口」**：它的作用只是「路线不可用（例如汽水服务起不来）时退回部分数据，而不是整条命令失败」。缺口仍然让退出码为 1，仍然写在 `ledger` 与 `warnings` 里——告诉用户「有缺口」这件事没有任何开关能关掉。
6. **抓取会打真实平台的接口**，默认请求间隔 1 秒、失败重试 2 次；批量任务不要并发压同一个平台。
7. 想看覆盖率就用 `mpa export --format csv`（每行都有 `*_source` 列）或 `mpa doctor --live`（报「声明可用但一条都没有」的字段）。**不猜语种**：网易云不给语种，那 200 条就是 `unavailable`。

## 已知的坑

- **`kuwo` 整条链路从未跑通**：没有公开可用的波点歌单 id，接口路径是从官方 H5 bundle 反查的；`mpa adapters`/`doctor` 都标着「未实测」。用户给了波点链接（`h5app.kuwo.cn/m/bodian/collection.html?uid=...&playlistId=...&source=5`）时，**先跑 `mpa doctor -p kuwo --live --id <playlistId>`** 确认路线，再把结果写回 `mpa/adapters/kuwo.py` 的 `verified_on`/`verified_note` 和 `docs/references/kuwo.md`。
- **汽水分享页硬上限 175 首**，超过部分只能起本地 `qishui-api` 服务；工具会靠「声明数 vs 实取数」发现截断并把退出码设成 1。
- **酷狗的 `gcid_*` 分享链接可以自动换算**：分享页仍内嵌歌单名/声明数/前 10 首，工具会用「歌单名 + uid 同时相符」的判据在 `collection_3_<uid>_<idx>_0`（idx 1..5）里探测明文 id（需要能起本地 node 服务）。探测不中时按报错提示换成带明文 id 的链接。
- **网易云没有官方语种字段**，热歌榜的 `popularity` 是常量 `100.0`（常量字段没有区分度，下游别拿它做比较）。
- **汽水（qishui）的 SSR 页面每次请求都不一样**（`node_uuid`、头像 CDN 域名、点赞数，甚至 `relation_media` 标签），所以 `raw_digest` 只能当次快照：`mpa verify` 证明的是「这份 raw 能复算出这份 playlist.json」，不是「这个链接每次抓都一样」。判断上游有没有变要比派生结果的形状，别比 raw 摘要。适配器只取稳定字段，所以两次抓取的派生结果**除了曲风**（`extra.genre`/`extra.subgenre`，见下一条）之外逐字节相同。
- **`extra` 记的是「抓的时候平台写的是什么」，不是曲目的固有属性。** 汽水同一首歌的 `Genre` 标签两次抓取能从 `Hip Hop/Rap` 摇成 `Pop`、二级标签摇成空。所以引用 `extra` 里的值时要说清是某个时间点的快照；`contract_diff.py` 对 `extra` 也只判「这个键是不是整批消失了」，不看覆盖率比例。
- **`extra` 有两个作用域，别混。** 曲目级在 `tracks[].extra`（`alias`/`trans_name`、`album_type`/`label`、`fileid`/`remark`/`bpm`、`genre`/`subgenre`/`composers`/`lyricists`），歌单级在 `playlist.extra`（`tags`、`created_at`/`updated_at`、`published_at`）。作用域不同是有意的：歌单标签混进曲目级会让人以为「每首歌都有这个标签」。`capabilities` 里曲目级直接写键名、歌单级带 `playlist.` 前缀。导出时曲目级 extra 压在 `extra` 一列里（`bpm=95;fileid=847;…`），歌单级另占 `playlist_*` 列——作用域不同，不要混着数。
- **写完或改完适配器，一定要把 fixture 的 `raw/` 摊开逐字段问一遍「平台给了这个，我的输出里有吗」。** 契约测试只能发现 `playlist.json` 变成别的样子（改坏了），发现不了**数据一直在原文里、只是没解析**：汽水的 `lang_codes`（65/65 曲目都有）曾被写死成 `unavailable`，`Genre` 标签（65/65 都有）曾被整段丢掉，两处都是「平台给了、我们扔了」。反向的错误同样存在（把派生值当成收到的响应记进 `raw/`），两个方向都得查。真不取的字段，要在 `docs/schema.md` 第 11 节写下理由——**写不出理由的，就是还没修完的 bug**。
- **`raw/` 一个字节没变、只是 `derive` 的解释逻辑改了**（漏读字段、单位错、键名错）时，用 `python3 tools/rederive_fixture.py <fixture 目录>` 重算 `playlist.json`——它会顺手跑 `verify --strict`。**上游响应本身变了就必须重新 `mpa fetch` 造一份新记录**，不许用这个工具把红的 fixture 变绿。
- **上游漂移怎么发现**：`mpa verify` 只会验证「入库的 raw」，永远不知道平台今天长什么样；真正问这个问题的是 `python3 tools/contract_diff.py tests/fixtures/<平台> <刚抓的目录>`（canary 每天自动跑）。它只比形状——曲目键、规范字段的 `source` 覆盖率、`extra` 里的附加字段还在不在、`ledger.declared`、请求计划——不比具体值。
- 只要改动了适配器或 fixtures，记得跑 `python3 tools/gen_readme_numbers.py`，否则 CI 的 `--check` 会失败；改了抓取输出要同步 `python3 tools/make_provenance.py <fixture 目录>`。
- **新增平台适配器时，`declared` 必须真的来自平台**（如 `trackCount`、`total_song_num`）。平台没给总数就让它 `ledger.declared = None`，绝不能用 `len(取到的)` 顶替——那正好把「静默截断」伪装成「完整」。

## 想扩展

新增平台：复制 `mpa/adapters/kuwo.py` 的形状，实现 `collect`（发请求 + 记录响应）和 `derive`（纯函数），在 `mpa/adapters/__init__.py` 注册，然后**必须**真跑一次公开歌单、把输出放进 `tests/fixtures/<平台>/`、写 `docs/references/<平台>.md`、填 `verified_on`。没有实测记录的适配器会被 `mpa adapters` 公开标成「未实测」——这是这个项目的底线，不要绕过它。

契约细节（`mpa/playlist@1` 字段、`Field` 语义、`ledger` 口径、退出码、字段可得性矩阵）见 `docs/schema.md`；每个平台的请求细节与失效症状见 `docs/references/`；哪些字段「平台给了但我们不取、为什么」见 `docs/schema.md` 第 11 节。
