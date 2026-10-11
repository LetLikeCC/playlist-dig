# 酷我音乐 / 波点（kuwo）

| 项 | 值 |
| --- | --- |
| 适配器 | `mpa/adapters/kuwo.py`；`name="kuwo"`，`label="酷我音乐（波点）"`，`version="2.0.0"` |
| 输入形态 | `https://h5app.kuwo.cn/m/bodian/collection.html?playlistId=<id>&source=<n>`；裸数字 id（`--provider kuwo`） |
| 路线 | `direct-http`（两步：歌单信息 + 分页曲目） |
| 最后验证 | **2026-10-03**（首次端到端跑通）：用户提供的波点歌单（**id 不记录**），声明 236 / 实取 202、gap 34 如实报出、`mpa verify --strict` 五项全过 |
| 上游接口 | `GET https://bd-api.kuwo.cn/api/service/playlist/info/<id>`；`GET https://bd-api.kuwo.cn/api/service/playlist/<id>/musicList` |

**复核状态：有一部分第一手数据了，但仍然没有 fixture。** 这两件事必须分开说：

- **2026-10-03 首次端到端跑通**（用户提供的波点歌单，id 不记录）：`playlist_info` + 5 页非空 + 1 页空页 = 6 次请求；声明 236 / 实取 202、`gap=34`、退出码 1、warning 原文见下节；`mpa verify --strict` 五项全过。因此下面标注「实测」的行为是跑过的。
- **但仍然没有 `tests/fixtures/kuwo/`**：没有另一方能离线复算的记录，所以 `mpa verify` 对酷我**没有**可用的 fixture，`mpa doctor --provider kuwo --live` 依然因为没有**公开**样例 id 而跳过（`mpa/doctor.py:SAMPLES["kuwo"] = None`，文案：「没有确认可用的公开样例 id（见 docs/references/kuwo.md），不拿猜的 id 去「验证」」）。要补上这块，需要一份**可以公开**的波点歌单。
- **继承观察，未复核**：来自 v1 笔记（H5 bundle 反查与当时的探测），本次没有重新验证——标注照旧。

字段契约、`Field` 语义、退出码见 [../schema.md](../schema.md)。

---

## 请求头/前置条件

请求头是这条路线唯一的技术门槛，缺一个就换不来数据（来自代码）：

| 头 | 值 |
| --- | --- |
| `plat` | `h5` |
| `ver` | 空字符串 |
| `devid` | `h5` |
| `Referer` | `https://h5app.kuwo.cn/m/bodian/collection.html` |
| `User-Agent` | iPhone Safari（`MOBILE_UA`） |

两个接口都带 `source=<n>` 与一个随机 `reqId=<uuid4>`：

```
GET {BASE}/service/playlist/info/{key}?source={source}&reqId={uuid4}
GET {BASE}/service/playlist/{key}/musicList?source={source}&pn={page}&rn=50&reqId={uuid4}
```

`BASE = https://bd-api.kuwo.cn/api`；`rn=50`（`PAGE_SIZE`），最多 15 页（`MAX_PAGES`）。

`source` 从分享链接的 `?source=` 取出，放进 `Ref.params`，因此每个 `raw/` part 的 URL 里都能看到当时用的是哪个值；链接没带时用 `DEFAULT_SOURCE = "5"`。**不要把 `source` 写死成 5**：不同 `source` 的响应可能不同（继承观察，未复核：v1 笔记提到波点分享链接带 `uid`/`playlistId`/`source=5`，`uid` 当前适配器不使用）。

命名空间前置条件（来自代码 + v1 笔记一致）：

- 本路线用的是**波点（bd-api）playlist id**，与 `www.kuwo.cn` 歌单 id **不是同一套命名空间**。传 `www.kuwo.cn` 链接会被 `resolve` 直接拒绝（`InputError`），因为那条路会返回 `{"code":200,"data":{}}` 这种「空成功」——最坏的失败模式（继承观察，未复核：v1 笔记记录）。
- 结果里出现空 `data` 时，`_validate()` 一律按 `KIND_NOTFOUND` 处理并提示「歌单不存在，或 id 属于 www.kuwo.cn 命名空间」。

---

## 字段与坑

`derive` 从 `playlist_info.data` 与各页 `music_list_XX.data.list` 拼曲目（来自代码）：

| 输出 | 来源 | 备注 |
| --- | --- | --- |
| `playlist.id` | `data.id`，缺失退 `ref.key` | |
| `playlist.title` | `data.name` | |
| `playlist.creator` | `creatorName` / `creatorId` | |
| `playlist.description` | `data.description` | |
| `declared_count` | `data.musicCount` | 与分页条数是两回事，见下节 |
| `track.id` | `item.id`（转字符串） | |
| `track.title` | `item.name`；为空则整位 `unavailable`，原因「接口返回了空曲目占位」 | |
| `track.artists` | `item.artists[]` 的 `name`/`id`；空时退到 `split_artists(item.artist)` | 两种形态都兼容 |
| `track.album` | `item.album`，可能是对象也可能是字符串 | 是字符串时当专辑名，`id` 为 `None` |
| `duration_ms` | `item.duration`（**秒**）×1000，`raw={"sec": duration}` | |
| `language` | `item.language`，曲目级 | 与 QQ 不同，酷我语种是逐曲字段 |
| `released_at` | `item.releaseDate` → `YYYY-MM-DD`，`raw` 保留原值；无值时 `unavailable("该曲目没有 releaseDate")` | |
| `collected_at` | 永远 `unavailable`（「酷我接口不返回收藏时间」） | |

坑：

1. **时长单位是秒。** 与 netease（毫秒）、kugou（毫秒）、qishui（毫秒）不同。改这个适配器时把秒当毫秒会让所有曲目变成 3 秒左右。
2. **`data.total` 与 `data.musicCount` 是两个数。** `musicCount` 进 `declared_count`，`total` 只用于分页判断和 warning 文案 `歌单声明 {declared} 首（接口 total={total}），实际取到 {n} 首`。
3. **分页终止条件有四个**（来自代码）：本页 `list` 为空、`ids ⊆ seen`（整页重复）、`len(seen) >= total`、`len(seen) >= declared`。前两个是「没有新东西了」，后两个是「取够了」。
3.1 **（2026-10-03 实测）分页标志是死的，别用它们当终止条件。** 响应里确实有 `firstPage`/`lastPage`/`hasNextPage`/`isLastPage`/`endRow`，但**第 1 页就全是 `0/false`**（`endRow` 也是 0），换成第 2 页同样如此。所以「还有下一页吗」只能靠**本页是否为空**判断——这正是当前实现的做法。
3.2 **（实测）单页条数可能少于 `rn`。** 同一份歌单 5 个非空页实测 48 / 41 / 39 / 44 / 30 条（`rn=50`），平台侧会滤掉一部分；因此「本页不满 50 条 = 最后一页」也是错的判断。第 6 页返回 `data.list == []`（`data` 本身在，不是空 `data`），循环才停下。
3.3 **（实测）`musicCount` 与分页实际能取到的量可以差很多。** 那份歌单 `musicCount = total = 236`，翻到空页时只有 202 首，`gap = 34`。工具不做任何补数：`declared` 保持 236、`gap` 记 34、退出码 1、warning 照写。
4. **`musicCount` 可能大于分页能取到的量。** 触发上限时（`len(tracks) >= 15 × 50 = 750`）写 warning `分页在上限 15 页处停止，可能仍有未取到的曲目`。
5. **`reqId` 每次都新**，所以同一歌单两次抓取的 `raw/` URL 不同；但由于 `raw_digest` 只对**响应体**取摘要，`mpa verify` 不受影响。

---

## 数量对不上的情况

| 现象 | 代码行为 |
| --- | --- |
| 接口没给 `musicCount` | `declared` 与 `gap` 都保持 `null`（**不用 `len(取到的)` 顶替**），`base.make()` 追加「平台没有给出曲目总数，只取到 N 首：这份数据无法证明完整（ledger.declared 为 null）」，退出码 1。此条来自代码；2026-10-03 的实测里 `musicCount` 有值，所以没走到这一支 |
| 分页出现重复曲目 | `dedupe_by_id()` 去重，`ledger.duplicates_removed` 记数，warning `分页去重移除 {n} 条重复曲目` |
| 去重后条数 ≠ `musicCount` | warning `歌单声明 {declared} 首（接口 total={total}），实际取到 {n} 首`；`declared` 仍取 `musicCount`，`ledger.gap > 0`，退出码 1。**2026-10-03 实测命中过这一条**：236 声明 / 202 实取 / gap 34 |
| 某页返回空 `data` | `_validate()` 直接抛 `FetchError(KIND_NOTFOUND)`，不会当成「翻完了」。**这一条与 kugou 不同**：kugou 用空页当正常终止，酷我用空 `data` 当异常 |
| 曲目 `name` 为空 | 该位 `status="unavailable"`，原因「接口返回了空曲目占位」，计入 `ledger.unavailable` |
| 取满 750 条 | warning「分页在上限 15 页处停止，可能仍有未取到的曲目」 |

---

## 失效症状

| 症状 | 抛出 | 退出码 |
| --- | --- | --- |
| HTTP 402 | `FetchError`（`error_hints` 文案「缺少 plat/devid 等必需请求头」） | 2 |
| 响应不是 JSON 对象 | `UpstreamChanged("kuwo: {route} 返回的不是 JSON 对象")` | 3 |
| `data` 为 `{}` 或 `null` | `FetchError(kind=KIND_NOTFOUND)`，文案「{route} 返回空 data（歌单不存在，或 id 属于 www.kuwo.cn 命名空间）」，`context={key, route}` | 2 |
| 输入是 `www.kuwo.cn` 链接 | `InputError`，文案点明命名空间不同并给出正确的链接形态 | 4 |
| 输入里解析不出 id | `InputError("kuwo: 无法从输入中解析歌单 id: …")` | 4 |

**402 是最有价值的一条线索**：它几乎总是请求头问题，而不是歌单问题。看到 402 先去改 `HEADERS`，不要去查 id。

---

## 失败路线备忘

| 路线 | 现状 |
| --- | --- |
| 抓 `collection.html` 页面本身（SPA 外壳） | 已废弃：该页面是 JS 壳，HTML 里没有歌单数据，必须走 `bd-api`（继承观察，未复核） |
| 第三方 node 服务取酷我 | 已废弃：v1对酷我也是 shell out 到第三方服务；本项目确认 `bd-api` 是纯 HTTP，因此不引入 node 依赖。**不要**为了「稳妥」把它改回服务路线 |
| `www.kuwo.cn` 的歌单 id | 永久拒绝：命名空间不同，返回空成功。已在 `resolve` 里挡住 |
| H5 bundle `https://h5s.kuwo.cn/m/bodian/collection8afe47c8.js` | 仅作逆向参考：接口路径与必需请求头是从这里反查出来的。bundle 文件名带哈希，随时会变，不要当稳定资源引用（继承观察，未复核） |

---

## 维护提示

这条路线**还没有被验证过**，所以维护动作与其它平台不同：先补验证，再谈回归。

```
# 1) 拿到一个真实的波点歌单 id（形如 collection.html?playlistId=... 里的数字）
mpa doctor --provider kuwo --live --id '<波点歌单 id>'   # 期望：声明数与实取数一致，四个字段覆盖率符合 capabilities
mpa fetch '<https://h5app.kuwo.cn/m/bodian/collection.html?playlistId=<id>&source=5>' --out /tmp/kw
mpa verify /tmp/kw
mpa export /tmp/kw --format csv | head -3
# 2) 验证通过后，把 mpa/adapters/kuwo.py 的 verified_on / verified_note 改成实测日期与结论，
#    并把该 id 填进 mpa/doctor.py 的 SAMPLES["kuwo"]（现在是 None，导致 --live 直接跳过）
# 3) 再把本文件顶部的「最后验证」与「复核状态」两栏改成实测结论
```

`mpa fetch` 返回 402、或 `mpa doctor --provider kuwo --live --id <id>` 报「返回空 data」＝**请求头或接口路径过期（前者）／id 命名空间不对（后者），重读本文件的「请求头/前置条件」与「失效症状」两节**。
