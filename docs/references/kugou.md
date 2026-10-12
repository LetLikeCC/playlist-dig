# 酷狗音乐（kugou）

| 项 | 值 |
| --- | --- |
| 适配器 | `mpa/adapters/kugou.py`；`name="kugou"`，`label="酷狗音乐"`，`version="2.0.0"` |
| 输入形态 | 明文 `collection_3_<uid>_<idx>_0`；`https://www.kugou.com/...` 歌单链接 |
| 路线 | node 服务（社区项目 `MakcRe/KuGouMusicApi`，`@9b6a779`）；`default_mode="node-service"`，`requires_node=True` |
| 最后验证 | 2026-09-29：「collection_3_2132041455_14_0：声明 50 / 实取 50，经由本地 KuGouMusicApi @9b6a779 服务」 |
| 上游接口 | 本地服务上的 `GET /playlist/detail?ids=<id>`、`GET /playlist/track/all?id=<id>&page=<n>&pagesize=300` |

复核状态：本文数字与结构均用 `tests/fixtures/kugou/` 重新核对（`mpa verify tests/fixtures/kugou` 通过，`raw` 2 项，`digest e8924c06…`）。fixture 的 `mode` 是 `node-service-external`（服务跑在 `http://127.0.0.1:3210`），因此**这次验证的是接口契约，不是自动下载安装那条路**；自动安装路径（`node-service-local`）本次未复核。

字段契约、`Field` 语义、退出码见 [../schema.md](../schema.md)。

---

## 请求头/前置条件

### 必须先有 node 服务

酷狗没有可用的纯 HTTP 路线，全部数据都从社区服务 `MakcRe/KuGouMusicApi` 取。工具自己管理这个服务（`mpa/adapters/node_service.py`）：

| 项 | 行为 |
| --- | --- |
| 固定版本 | `commit=9b6a779abfc9b4590ed671836c82db398fde5929`，入口 `app.js`，健康检查 `/` |
| 下载 | `https://codeload.github.com/MakcRe/KuGouMusicApi/tar.gz/9b6a779…`，落到 `PLAYLIST_DIG_HOME`（默认 `~/.cache/playlist-dig`）的 `services/` 下 |
| 安装 | 有 `pnpm` 且 checkout 里有 `pnpm-lock.yaml` → `pnpm install --frozen-lockfile`（`install_method="pnpm-frozen-lockfile"`）；否则 `npm install --no-audit --no-fund`（`install_method="npm-install-unpinned"`）；`node_modules` 已存在 → `cached` |
| 启动 | 绑定 `127.0.0.1` + 随机空闲端口，`HOST`/`PORT`/`NODE_ENV=production`；就绪等待 45 s，安装超时 900 s |
| 失败 | 抛 `RouteUnavailable` 并给出 `MANUAL_HINT`：自己起一个服务后用 `--service-url http://127.0.0.1:<port>` |

缺 node/npm、或加了 `--no-node` 又没有 `--service-url`，同样是 `RouteUnavailable`（退出码 5）。

注意 `install_method="npm-install-unpinned"` 含义：**服务源码的 commit 是钉住的，npm 依赖树不是**。仓库没有 lockfile 时无法钉住依赖，这个值就是标记。看到它时，「同一个 commit 两次跑出不同结果」是可能的，先怀疑依赖。

### 外部服务：`--service-url`

传 `--service-url` 时工具先 `_check_reachable()` 探活，成功则记 `mode="node-service-external"`，**不再下载也不再安装**。此时 `install_method` 与 `resolved_digest` 都是 `null`。

**这里有一个容易误读的地方：`upstream_commit` 仍然写着 `9b6a779…`，但那是工具钉住的版本，不是外部服务实际跑的版本。** 适配器写的是 `ctx.upstream_commit = handle.upstream_commit or SERVICE.commit`，外部服务没有版本信息（`handle.upstream_commit` 为 `None`），于是回落到钉住的 commit。它的意思是「这份数据是按 `@9b6a779` 的接口契约验证的」，不能当作「服务就是那个版本」的证据。fixture 的 `ctx.extra.service` 里 `mode=node-service-external`、`install_method=null`、`resolved_digest=null`、`upstream_commit=null`，而 `playlist.json.source.upstream_commit` 与 `fetch.log.json.upstream_commit` 都是钉住值——三处的差异就是这条注脚。最极端的对照是汽水：它这一份走 SSR、根本没起服务，`playlist.json` 里没有 `upstream_commit` 键，日志里却仍有 `e409c10f…`（= `adapter.upstream_commit`）。三个位置的完整对照表见 [../schema.md](../schema.md) 第 5.1 节末。

### 歌单身份必须是明文 id

| 输入 | 结果 |
| --- | --- |
| `collection_3_<uid>_<idx>_0` | 通过。`/top/playlist` 会把同一个值作为 `global_collection_id` 返回，这是唯一可靠的取法 |
| 纯数字 `specialid` | `InputError`：只认明文 id；数字会得到空 data（空成功），并提示从 `/top/playlist` 的 `global_collection_id` 取 |
| `gcid_xxx` 分享链接 | 三步：① 从 `m.kugou.com/songlist/gcid_xxx/` 页面里正则找明文 id；② 找不到就**探测** `collection_3_<uid>_<idx>_0`（idx 1..5），判据是「歌单名相同 **且** `list_create_userid` 等于链接里的 uid」（`_probe_collection_id()`，需要本地 node 服务）；③ 仍不中才 `InputError`，并说清卡在哪一步、试过哪些 idx |

`_share_lookup()` 是**尽力而为**：urllib 直连、20 s 超时、任何异常都吞掉，UA 用 iPhone/微博 UA。它是历史路线的残留通道，不要把它当主路线，也不要因为它失败就认为 id 不存在。

### `gcid_*` 分享页到底是什么样（2026-10-03 实测两份真实链接）

**「分享页返回空壳」这个说法只对了一半，别再照着它写代码或写文档。** 实测：页面**仍然**内嵌
`window.$output`，里面有

* `info.listinfo`：歌单名、`count`（平台声明数）、`list_create_userid` / `list_create_username`、
  `heat`、`musiclib_tags`、`is_pri` / `is_publish` / `no_show_gid`；
* `info.songs`：**只有前 10 首**（形状与 `/playlist/track/all` 的曲目对象一致），没有分页字段。

真正为空的是 **`global_collection_id`**（以及 `list_create_gid`）：当歌单带 `no_show_gid=1`
（典型例子：用户自己的「喜欢的音乐」）时平台不在页面上给明文 id。上游 `KuGouMusicApi` 的代码里
**`gcid` 零命中**（做过代码搜索），`/playlist/detail` 只认 `global_collection_id`，所以页面上
那条捷径是走不通的——但这不等于取不到，见下一节。

### 明文 id 藏起来时：用「有验证的探测」找回来（v1 的老办法，v2 已搬回）

`collection_3_<uid>_<idx>_0` 里的 `idx` 是**歌单在该用户歌单列表里的位置**（v1 的实测：默认收藏
通常是 1、喜欢的音乐常在 1~2）。于是可以在一个很小的范围里试，并用**身份校验**把「试」变成
「验证」：

```
for idx in 1..5:
    cid = collection_3_<uid>_<idx>_0
    row = GET {service}/playlist/detail?ids=<cid>   # data[0]
    命中当且仅当  row.name == 分享页的 listinfo.name
              且  str(row.list_create_userid) == 链接里的 uid
```

两条**同时**满足才算命中：名字单独相同不算——不同用户完全可以给歌单起同一个名字。
2026-10-03 实测（用户提供的歌单，**id 与 uid 不记录**）：idx 1 未命中、**idx 2 命中**，
随后取到 **230 首（声明 230 / 实取 230，其中 2 条是已下架空位 → `ledger.unavailable=2`，
`gap=0`，退出码 0）**，`mpa verify --strict` 五项全过。

三个必须写清的限制：

* **探测要能起本地 node 服务**（`/playlist/detail` 只有服务提供）。服务起不来（没装 node、
  `--no-node`、端口不通）时探测安静放弃，报错照旧给出「换明文 id 链接」的指引。
* **探测范围只有 idx 1..5**，且**逐个都要过身份校验**；没命中就是没命中，不会无限试——
  那才会变成「猜 id」。
* **探测请求不进 `raw/`**：它们是「为了找到 id」的临时请求，不是这份数据的证据。实现走的是
  裸 `urllib`，而不是带记录的 `HttpClient`——否则「记进 `raw/` 的必须等于交给 `derive` 的」
  这条不变量会被探测请求污染。

没命中时，报错会带上页面里读到的歌单信息（名字、声明数、创建者、内嵌几首）+「试过 idx 1..5
没有一份名字与 uid 同时对得上」，而不是甩一句「页面是空壳」。

---

## 字段与坑

### 曲目字段

fixture 实测：50 首，`playlist_detail`（1450 B）+ `track_all_01`（118456 B）两个 part。

| 输出 | 来源 | 备注 |
| --- | --- | --- |
| `track.id` | `hash`，退 `mixsongid`，再退 `audio_id` | fixture 是 `hash`（32 位大写十六进制） |
| `track.title` | `songname`，退 `filename`；都没有则是下架占位 | |
| `track.artists` | `singerinfo[]` 的 `name`/`id`；为空时用 `split_kugou_name(raw_name)` 从 `"歌手 - 标题"` 拆 | 拆不出来就返回空歌手列表，标题保持原样 |
| `track.album` | `albuminfo` 的 `name`/`id`，`release_date` 取 `publish_date` | |
| `duration_ms` | `timelen`（**毫秒**），退 `duration`；`raw={"timelen":…, "duration":…}` | `duration` 恒为 `null` |
| `language` | `song.language`，曲目级 | |
| `released_at` | `publish_date` → `YYYY-MM-DD`，`raw` 保留原字符串 | |
| `collected_at` | `collecttime`（Unix **秒**）→ `YYYY-MM-DD`，`raw={"collecttime":…}` | 酷狗是五个平台里唯一有收藏时间的 |
| `extra.fileid` | `song.fileid` | 平台额外字段，导出 CSV 时压在 `extra` 列里（`fileid=847;…`） |
| `extra.remark` | `song.remark` | 50/50。平台写在曲目上的说明串（`《星曜计划》音乐企划` 9、`《繁星互娱》厂牌` 4……），38 种取值 |
| `extra.bpm` | `song.bpm` | 50/50，平台自己算的速度值，观测范围 56–177 |

坑：

1. **`duration` 永远是 `null`，真实值在 `timelen`。** 直接读 `duration` 会得到整表无时长。适配器写成 `clean_int(timelen) or clean_int(duration)`，两者都空才标 `unavailable`。
2. **`track_all` 的响应可能是对象也可能是裸数组**（`data.songs` 或 `data` 本身）。`collect` 两种都收，形状不对才抛 `UpstreamChanged`。
3. **`playlist_detail` 的 `data` 是数组**（取 `data[0]`）。fixture 实测 `data[0]` 的键：`name`、`count`、`collect_total`、`global_collection_id`、`list_create_username`、`list_create_userid`、`intro`、`musiclib_tags`、`specialid`、`source`、`number`、`listid`、`publish_date`、`update_time`。注意 `specialid` 在里面，但**不要**拿它当输入用。
4. **`track_all` 的 `data.count` 与 `playlist_detail` 的 `count` 是两个数**：前者进 warning 文案，后者进 `declared_count`。
5. **`collecttime` 是秒。** 与 `timelen`（毫秒）同在一个对象里，单位不同，改代码时不要合并处理。
6. fixture 里 50 首的 `collected_at` 全部是 `2026-02-13`——**这是个常数**，任何基于它的「发现」都只是这个常数本身。取值是对的（平台给的就是这个），但下游别拿它当有区分度的字段用。

### 元信息

| 输出 | 来源 |
| --- | --- |
| `playlist.id` | `global_collection_id`，退 `ref.key` |
| `playlist.title` | `name` |
| `playlist.creator` | `list_create_username`，退 `nickname`；id 取 `list_create_userid` |
| `playlist.description` | `intro` |
| `declared_count` | `count` |
| `playlist.extra.tags` | `musiclib_tags[].tag_name` → `["流行", "网络", "甜蜜", "精选"]`，`raw={"tag_ids": [9, 35, 47, 1084]}` |
| `playlist.extra.created_at` / `updated_at` | `create_time` / `update_time`（秒 epoch）→ `2026-01-29T15:14:20Z` / `2026-02-13T12:19:44Z` |
| `playlist.extra.published_at` | `publish_date`（平台本来就写成 `YYYY-MM-DD`，原样透传） |

歌单标签有**两个来源**：结构化的 `musiclib_tags[]` 和一个逗号串 `tags`（`"流行,网络,甜蜜,精选"`）。适配器只取结构化的那一份——同一信息两个来源迟早会互相打架，多存一份不增加信息量。

不取：`collect_total` 与 `heat` 在这份记录里都是 `0`（社交计数，且全 0 会被误读成「热度为零」）；曲目级 `tagmap.genre0` 是**位掩码**（`524288`/`526336`…），要一张平台侧的映射表才读得懂，我们不自己造那张表；`extname`/`bitrate`/`level`/`relate_goods`/`download`/`feetype`/`privilege` 是音频格式与下载权限。理由见 [../schema.md](../schema.md) 第 11 节。

fixture 实测：创建者「酷狗文化」（`list_create_userid 2132041455`，正好是 id 里的 uid 段）；`playlist.title` 与 `playlist.id` 的原文见 `tests/fixtures/kugou/playlist.json`（标题本身含平台自带标点，此处不转写以免与原文不符）。

---

## 数量对不上的情况

| 现象 | 代码行为 |
| --- | --- |
| 接口没给 `count` | `declared` 与 `gap` 都保持 `null`（**不用 `len(取到的)` 顶替**），`base.make()` 追加「平台没有给出曲目总数，只取到 N 首：这份数据无法证明完整（ledger.declared 为 null）」，退出码 1 |
| 曲目数 ≠ `count` | warning `歌单声明 {declared} 首（track/all 报告 count={reported}），实际取到 {n} 首（原样 {before} 条）`；三个数都给出来，便于区分「接口重复」与「接口少给」 |
| 分页出现重复 | `dedupe_by_id()`，`ledger.duplicates_removed` 记数，warning `分页去重移除 {n} 条重复曲目` |
| 空页 | 作为正常终止（`break`），**不报错**。这一点与酷我相反（那边空 `data` 是 `KIND_NOTFOUND`） |
| 取满上限 | `MAX_PAGES(10) × PAGE_SIZE(300) = 3000` 条时 warning「分页在上限 10 页处停止，可能仍有未取到的曲目」 |
| 曲目被下架（只留空位） | 该位 `status="unavailable"`，原因「该曲目已被下架（接口只留空位）」，计入 `ledger.unavailable` |

分页循环的终止条件是四个：空页、整页重复（`ids ⊆ seen`）、`len(seen) >= declared`、页数到 10。fixture 的 50 首在第一页就满足了 `len(seen) >= declared`，所以只有 `track_all_01`。

---

## 失效症状

| 症状 | 抛出 | 退出码 |
| --- | --- | --- |
| 服务返回 `{"data":{},"status":1}`（数字 id 或 `gcid` 当 id 用） | `FetchError(kind=KIND_NOTFOUND)`，文案「playlist/detail 返回空 data（歌单不存在，或 id 不是 collection_ 明文形式）」，`context` 带 `key`/`status`/`error_code` | 2 |
| 路由 404 | `FetchError`，`error_hints` 文案「该路由不存在」 | 2 |
| `playlist/detail` 不是 JSON 对象 | `UpstreamChanged("kugou: playlist/detail 返回的不是 JSON 对象")` | 3 |
| `track_all` 结构异常 | `UpstreamChanged("kugou: track_all_NN 返回结构异常")` | 3 |
| 缺 node/npm 且未给 `--service-url` | `RouteUnavailable`（提示装 node 或用 `--service-url`） | 5 |
| 服务 45 s 内没就绪 | `RouteUnavailable`，`context` 带服务日志路径 | 5 |
| 下载/安装失败 | `RouteUnavailable` | 5 |
| `gcid` 链接三步都失败（页面没数据 / 服务起不来 / 探测 1..5 全不中） | `InputError`，文案写清卡在哪一步、试过哪些 idx，并给出明文 id 的形态 | 4 |
| 纯数字 id | `InputError`，文案说明会得到空成功 | 4 |

排障顺序建议：先看 `fetch.log.json` 的 `mode`——`node-service-local` 出问题看服务日志；`node-service-external` 出问题看那个 `--service-url` 服务本身，工具没管它。

---

## 失败路线备忘

| 路线 | 现状 |
| --- | --- |
| `m.kugou.com/songlist/gcid_xxx/` 页面解析 | 页面**有**数据（`window.$output`：`listinfo` + 前 10 首）；`global_collection_id` 可能是空串（`no_show_gid=1`），此时改走「idx 1..5 × 名字+uid 校验」的探测（见上文），实测能命中。仍**不要**把它当主路线 |
| 数字 `specialid` 直查 | 永久拒绝：服务返回空成功。已在 `resolve` 里挡住 |
| 裸 `gcid_xxx` 当 id 传 | 同上：先查页面，再探测，都不成才报错 |
| v1 笔记里的 `cd kugou-api && npm ci` | 不可用：仓库没有 `pnpm-lock.yaml`/`package-lock.json` 时 `npm ci` 直接失败。工具改为「钉 commit + `pnpm install --frozen-lockfile` 或 `npm install`」，并把实际用了哪种记进 `install_method` |
| `duration` 字段 | 恒为 `null`，永远用 `timelen` |

---

## 维护提示

```
mpa doctor --provider kugou --live --id collection_3_2132041455_14_0    # 会自动起本地服务，较慢
mpa fetch collection_3_2132041455_14_0 --out /tmp/kg                  # 走自动下载安装
mpa fetch collection_3_2132041455_14_0 --service-url http://127.0.0.1:3210 --out /tmp/kg   # 用自己起的服务
mpa verify /tmp/kg
mpa export /tmp/kg --format csv | head -3                              # 确认 collected_at 仍在每一行里
```

`mpa doctor --provider kugou --live --id collection_3_2132041455_14_0` 报出「返回空 data」「路由不存在」或四个字段覆盖率骤降＝**社区服务 `KuGouMusicApi` 的接口变了（或钉住的 commit 已过期），重读本文件的「字段与坑」与「失效症状」两节**；先试更新 `SERVICE.commit` 并重新核对 `/playlist/detail` 与 `/playlist/track/all` 的响应形状。
