# 网易云音乐（netease）

| 项 | 值 |
| --- | --- |
| 适配器 | `mpa/adapters/netease.py`；`name="netease"`，`label="网易云音乐"`，`version="2.0.0"` |
| 输入形态 | 裸数字 id；`https://music.163.com/#/playlist?id=<id>`；`https://music.163.com/playlist/<id>`；`https://music.163.com/discover/toplist?id=<id>` |
| 路线 | `direct-http`（两步：歌单详情 + 分批曲目详情） |
| 最后验证 | 2026-09-29：「网易云热歌榜 3778678：声明 200 / 实取 200」（`verified_on` / `verified_note`） |
| 上游接口 | `POST https://music.163.com/api/v3/playlist/detail`；`GET https://music.163.com/api/song/detail?ids=[...]` |

复核状态：本文所有数字与结构都用 `tests/fixtures/netease/` 的原始响应重新核对过（`mpa verify tests/fixtures/netease` 通过）。凡属继承自 v1 笔记、本次无法复现的说法，在文中标「继承观察，未复核」。

字段契约、`Field` 语义、退出码见 [../schema.md](../schema.md)。

---

## 请求头/前置条件

| 项 | 值 |
| --- | --- |
| 详情请求 | `POST`，body `{"id": "<key>", "n": "1000"}`（表单编码，`n` 是字符串） |
| 曲目请求 | `GET`，`?ids=` 后跟 URL 编码的 JSON 数组 |
| 必需请求头 | `Referer: https://music.163.com/`（两个接口都带） |
| 分批 | `SONG_CHUNK = 100`，part 名 `song_detail_000`、`song_detail_001`、… |
| 登录 | 不需要。公开歌单与榜单可直接读 |

fixture 里的请求记录（`fetch.log.json` 的 `requests`，共 3 条）：`playlist_detail_v3` 476009 B、`song_detail_000` 313462 B、`song_detail_001` 302807 B，全部 HTTP 200。

**`n=1000` 是上限而非保证**：详情接口一次最多返回约 1000 条 `trackIds`，超过的榜单要靠 `trackIds` 之外的分页手段，当前适配器没有实现。这是已知局限，不是 bug。（继承观察，未复核：v1 笔记提到更大的榜单会被截断，本次 fixture 只有 200 首，触发不到。）

---

## 字段与坑

### 曲目顺序以 `trackIds` 为准

`derive` 只从 `playlist.trackIds` 取 id 列表并保持其顺序；只有当 `trackIds` 完全缺失时才退到 `playlist.tracks[].id`，两者都拿不到就抛 `UpstreamChanged`（见「失效症状」）。2026-09-29 的 fixture 里 `trackIds` 200 条、内嵌 `tracks[]` 也是 200 条且顺序完全一致——但**不要**依赖内嵌数组：v1 笔记记录过它被截断的情况（继承观察，未复核）。

### v3 内嵌曲目与 `song/detail` 的字段名不同

同一首歌在两个响应里的键名不一样，适配器只解析 `song/detail` 的那一套：

| 语义 | 内嵌 `tracks[]` | `song/detail` `songs[]` |
| --- | --- | --- |
| 时长 | `dt` | `duration` |
| 歌手 | `ar` | `artists` |
| 专辑 | `al` | `album` |
| 热度 | `pop` | `popularity` |
| 发行时间 | `publishTime` | `publishTime`（曲目级，见下） |

因此调试时看错了对象会得出「字段消失」的错误结论。fixture 中内嵌曲目的键实测为 `['a','al','ar','dt','id','name','pop','publishTime',…]`。

### `released_at` 的取值链

```
publish = album.get("publishTime") or song.get("publishTime")
released_at = Field.platform(iso_date(publish), raw=publish) if iso_date(publish) else unavailable("released_at")
```

即**专辑级优先、曲目级兜底**，两者都缺才 unavailable。2026-09-29 的 200 首里：`album.publishTime` 缺失 0 条；曲目级 `publishTime` 全部存在但有 37 条值为 `0`（`0` 为假值，所以那 37 条实际用的是专辑级时间）；`released_at` 覆盖率 200/200、`source` 全是 `platform`。`Field.raw` 保留毫秒时间戳，例如 `{"value":"2022-08-19","raw":1660924800000}`。

### 单位

`duration` 是毫秒，直接写入 `duration_ms`，不做换算（fixture 首曲 295940）。

### 热度是饱和值，不能当指标

`song/detail` 的 `popularity` 在热歌榜里 200/200 全为 `100.0`——**这是个饱和常数**。它照样进 `extra.popularity`（平台给了就取），但任何「平均热度 100」的结论都只是这个常数本身，不是发现。

### 曲目级专属字段：别名、译名

| `extra` 键 | 来源 | fixture 实测 |
| --- | --- | --- |
| `alias` | `song/detail` 的 `songs[].alias` | 39/200。别名数组整段留下（如《海屿你》→ `["求你别离开我"]`），不拼接、不去重 |
| `trans_name` | `songs[].transName` | 17/200（如《明知故犯》→ `You know that I feel bad`） |

两者都是平台写的字符串，原样进 `extra`，不做任何加工。**不要把它们当成「官方译名」的权威来源**——平台对哪些歌给译名是不一致的。

### 歌单级专属字段：创建/更新时间

`playlist.createTime` / `updateTime` / `trackUpdateTime` 都是**毫秒** epoch，写进 `playlist.extra` 时值换成 UTC ISO、`raw` 留原数：

| 键 | fixture（热歌榜） |
| --- | --- |
| `created_at` | `2013-09-09T10:10:06Z`（raw `1378721406014`） |
| `updated_at` | `2026-09-29T00:22:25Z`（raw `1790641345899`） |
| `track_updated_at` | `2026-09-29T12:32:09Z`（raw `1790685129309`） |

`playCount`（14159011840）、`subscribedCount`、`commentCount`、`shareCount` 与 `tags`/`displayTags` 都**不取**：前四个是每秒都在变的社交计数，后三个在这份记录里是空数组/`null`（平台这次没给标签）。理由见 [../schema.md](../schema.md) 第 11.5 节。

### 平台没有的字段

| 字段 | 情况 |
| --- | --- |
| `language` | 网易云不返回语种。`capabilities.language=false`，200/200 为 `unavailable`，原因文案「网易云不返回语种字段（旧版按歌名猜语言属于启发式，不属于数据）」 |
| `collected_at` | 不返回，原因文案「网易云不返回收藏时间」 |

**本仓库不给语种猜测**：网易云响应里没有语种字段，所以这 200 条的 `language` 就是 `unavailable` + 原因，`capabilities` 里也没有 `language`。想按语种分类属于下游分析的事，而且要先接受「那是猜的」。

### 元信息从 v3 响应的 `playlist` 取

| 输出 | 来源 |
| --- | --- |
| `playlist.id` | `playlist["id"]`，缺失退到 `ref.key` |
| `playlist.title` | `playlist["name"]`，再退 `playlist["title"]`。**v3 实际用 `name`，`title` 在这个接口里不存在** |
| `playlist.creator` | `playlist.creator.nickname` / `playlist.creator.userId`，userId 缺失退 `playlist.userId` |
| `playlist.description` | `playlist["description"]` |
| `playlist.declared_count` | `playlist["trackCount"]`。**缺失时保持 `null`，不退 `len(trackIds)`**（旧代码有 `or len(...)` 兜底，已删） |

fixture 实测：`id=3778678`，`name=热歌榜`（`title` 键不存在），`creator={nickname: 网易云音乐, userId/userId: 1}`，`declared_count=200`。

---

## 数量对不上的情况

| 现象 | 代码行为 |
| --- | --- |
| `trackCount` 缺席 | `declared` 与 `gap` 都保持 `null`，`base.make()` 追加「平台没有给出曲目总数，只取到 N 首：这份数据无法证明完整（ledger.declared 为 null）」，退出码 1。`trackIds` 照常取，数据仍可用，只是**无法证明完整** |
| `trackCount`（声明）≠ `len(trackIds)` | 写一条 warning：`歌单声明 {declared} 首，接口只返回 {len(ids)} 条 trackIds（差额见 ledger.gap）`；`declared` 仍取 `trackCount`，于是 `ledger.gap = declared - fetched > 0`，退出码 1 |
| `bannedTrackIds` 非空 | 写一条 warning：`接口标记 {n} 首为下架/屏蔽（bannedTrackIds）` |
| 声明了的 id 在 `song/detail` 里没有对应歌曲 | 该位生成 `status="unavailable"`，原因二选一：`已被下架或屏蔽（bannedTrackIds）`（id 在 banned 集合里）或 `playlist 声明了该曲目，但 song/detail 没有返回`。它计入 `ledger.unavailable` 与 `ledger.reasons`，不计入 `gap` |
| 同一 id 在 `song/detail` 里重复出现 | `songs` 以 id 为键建字典，后者覆盖前者，不产生重复曲目 |

顺序不变式：`tracks[i].index == i`，与 `trackIds` 一一对应；`status=unavailable` 的位保留空位而不是删除，否则与平台侧位置错位。

---

## 失效症状

| 症状 | 抛出 | 退出码 |
| --- | --- | --- |
| 响应不是 JSON 对象 | `UpstreamChanged("netease: playlist/detail 返回的不是 JSON 对象")` | 3 |
| `code` 为 401 或 403（需要登录、歌单受限） | `FetchError(kind=KIND_PRIVATE, context={"code":…})` | 2 |
| `code` 为 404 或 -1 | `FetchError(kind=KIND_NOTFOUND, context={"code":…})` | 2 |
| 其他 `code`（不是 200/None），或 `playlist` 不是对象 | `UpstreamChanged("netease: playlist/detail 结构异常 (code=…)")` | 3 |
| `trackIds` 与 `tracks[]` 都取不到 id | `UpstreamChanged("netease: playlist 中没有 trackIds - 接口结构可能已变化")` | 3 |
| `song/detail` 响应没有 `songs` 键 | `UpstreamChanged("netease: song/detail 返回结构异常 (chunk N)")` | 3 |
| 输入里解析不出 id | `InputError("netease: 无法从输入中解析歌单 id: …")` | 4 |

注意 401/403 与 404/-1 被有意区分：前者是「有歌单但读不到」（可重试/需登录），后者是「歌单不存在」（重试无用）。

---

## 失败路线备忘

以下路线**已经死掉或不再使用，不要删掉这段**，否则下一个人会重新踩一遍：

| 路线 | 现状 |
| --- | --- |
| `GET /api/playlist/detail`（旧版详情接口） | 已废弃：返回 `code 20001`，不返回 `playlist`。当前代码只用 `POST /api/v3/playlist/detail`（继承观察，未复核：v1 笔记记录） |
| 从歌名推算发行年份的旧公式 | 已删除。发行时间现在只来自 `publishTime`，算不出来就是 `unavailable`，绝不猜 |
| 日志里带 `Referer` 之外的伪装头 | 不需要。网易云这两个公开接口只要求 `Referer`，多加头只会让「请求头过期」变成新的故障源 |

`#/playlist?id=` 形式（hash 路由）由 `_ID_PATTERNS` 的 `[?&#]id=(\d+)` 覆盖，因此分享出来的带 `#` 链接也能解析。

---

## 维护提示

```
mpa doctor --provider netease --live --id 3778678    # 联网自检：声明数与实取数、字段覆盖率
mpa fetch https://music.163.com/#/playlist?id=3778678 --out /tmp/ne
mpa verify /tmp/ne                                   # 离线复算，必须通过
mpa export /tmp/ne --format csv | head -3            # 确认 popularity 进 extra、language 是 unavailable
```

`mpa doctor --provider netease --live --id 3778678` 报出 `released_at` 覆盖率下降、或 `language` 从「平台没有」变成「部分缺失」＝**上游改了曲目详情结构，重读本文件的「字段与坑」与「失效症状」两节**。
