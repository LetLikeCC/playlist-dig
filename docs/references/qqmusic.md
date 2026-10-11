# QQ 音乐（qq）

| 项 | 值 |
| --- | --- |
| 适配器 | `mpa/adapters/qq.py`；`name="qq"`，`label="QQ 音乐"`，`version="2.0.0"` |
| 输入形态 | 裸数字 id；`https://y.qq.com/n/ryqq/playlist/<id>`；任何带 `?disstid=<id>` 的链接 |
| 路线 | `direct-http`（两步：歌单 + 专辑详情批量） |
| 最后验证 | 2026-09-29：「公开歌单 7011264340：声明 76 / 实取 76（含专辑详情批量请求）」 |
| 上游接口 | `GET https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg`；`POST https://u.y.qq.com/cgi-bin/musicu.fcg` |

复核状态：本文数字与结构均用 `tests/fixtures/qq/` 重新核对（`mpa verify tests/fixtures/qq` 通过）。继承自 v1 笔记的说法在文中标「继承观察，未复核」。

字段契约、`Field` 语义、退出码见 [../schema.md](../schema.md)。

---

## 请求头/前置条件

### 第一步：歌单本体

`GET`，query 参数（`collect` 里写死）：

```
type=1&json=1&utf8=1&onlysong=0&disstid=<key>&begin=0&num=999
&loginUin=0&hostUin=0&format=json&inCharset=utf8&outCharset=utf-8
&notice=0&platform=yqq.json&needNewCode=0
```

请求头只需 `Referer: https://y.qq.com/`。不需要登录，公开歌单可直接读。`num=999` 是上限而非保证（继承观察，未复核：v1 笔记记录过超长歌单被截断，本次 fixture 76 首触发不到）。

part 名固定为 `playlist_fcg`，存整个 JSON 响应（不是 `cdlist[0]`，这样离线也能复核外层 `code`）。

### 第二步：专辑详情批量

`POST https://u.y.qq.com/cgi-bin/musicu.fcg`，头：

| 头 | 值 |
| --- | --- |
| `Referer` | `https://y.qq.com/` |
| `Content-Type` | `application/x-www-form-urlencoded` |

body 是**一层**的 JSON 对象：`{"comm": {...}, "req_0": {...}, "req_1": {...}, …}`，每批最多 `ALBUM_BATCH = 30` 张专辑：

```json
{"comm": {"ct": 19, "cv": 1873, "uin": "0"},
 "req_j": {"module": "music.musichallAlbum.AlbumInfoServer",
           "method": "GetAlbumDetail",
           "param": {"albumMid": "<albummid>"}}}
```

**最容易踩的坑：不要把它包成 `{"data": "<json 字符串>"}`。** 那样上游返回一个空对象，看起来「请求成功但没有任何字段」。适配器的模块注释专门点了这个失败模式（继承观察，未复核：v1 笔记记录）。

### 请求体必须存进 `raw/`

响应里的键是**位置化**的 `req_0..req_n`，只有请求体才能说明哪个响应属于哪张专辑。因此适配器用 `record_body=True` 把每个批次的请求体也存成一个 part：`album_detail_000_req`（`status` 为 `null`，它不是一次响应），响应是 `album_detail_000`。`derive` 用 `_request_mids()` 从记录下来的请求体恢复 albummid 顺序；解析不出来就说明「没有语种/没有发行日期」，并写进 warnings，而不是悄悄降级。

fixture 实测：4 次 HTTP 请求（`playlist_fcg` + 3 个专辑批次），7 个 raw part（3 个响应 + 3 个请求体 + 1 个歌单响应）。76 首歌对应 70 个不同的 albummid，按 30/30/10 分三批。

---

## 字段与坑

### `songlist[]` 的形状

fixture 中 `songlist[0]` 的键：`songname`、`songmid`、`interval`、`albummid`、`albumname`、`singer`、`songid`、`size128`…。**没有 `album` 子对象**（实测 `song.get("album")` 为 `None`），所以专辑 mid 要走 `song["album"]["mid"] or song["albummid"]` 这条兼容式——线上哪天补上子对象也不会坏。

| 输出 | 来源 |
| --- | --- |
| `track.id` | `songmid`，缺失退 `songid` |
| `track.title` | `songname`；为空则整位标 `unavailable`，原因「接口返回了空曲目占位」 |
| `track.artists` | `singer[]` 的 `name`/`mid`；一个都没有时占位 `<QQ 音乐未返回歌手>` |
| `track.album.name` | `song["album"]["name"] or song["albumname"]` |
| `track.album.id` | 专辑 mid（用于查语种/发行日期） |
| `track.album.release_date` | 专辑详情的 `publishDate`，归一化为 `YYYY-MM-DD` |
| `duration_ms` | `interval`（**秒**）×1000，`raw={"sec": interval}` |
| `language` | 专辑详情 `basicInfo.language`，`raw={"granularity":"album"}` |
| `released_at` | 专辑详情 `basicInfo.publishDate`，`raw={"publishDate":…, "granularity":"album"}` |
| `collected_at` | 永远 `unavailable`（「QQ 音乐接口不返回收藏时间」） |

### 语种是专辑级字段

`basicInfo.language` 属于专辑，不是曲目。适配器把这一点写进 `Field.raw`（`granularity: album`），并**无条件**追加一条 warning：

```
语种为专辑级字段，同一专辑内所有曲目相同：英语 40、纯音乐 24、国语 10、其他 2
```

（fixture 实测，取前 4 名。）含义很实际：同一专辑里 12 首歌会共享同一个语种值，不能把这 76 条 `language` 当成 76 个独立样本；所以「语种覆盖率」的分母是取到的曲目数，而那 76 条里有 12 条是同一张专辑的同一首歌。用同一专辑内不同曲目做「语种一致性」分析是无效的。

2026-09-29 的 70 张专辑里，`language` 与 `publishDate` 缺失均为 0。

### 每张专辑只请求一次

去重后的 albummid 按首次出现顺序批量请求，所以「曲目数 76、专辑数 70」是正常的：6 首歌属于重复出现的专辑。批次数 = `ceil(70/30) = 3`。

### 曲目级专属字段：专辑类型与厂牌（同样是专辑级）

| `extra` 键 | 来源 | fixture 实测 |
| --- | --- | --- |
| `album_type` | `basicInfo.albumType` | 76/76：`Single` 61、`录音室专辑` 10、`EP` 5（**中英混排是平台原文**，不翻译也不归一） |
| `label` | `data.company.name` | 75/76：`独立发行` 71，`Astralwerks`/`白桃文化`/`腾讯音乐人`/`阿甘工作室` 各 1；1 张专辑没给 `company` |

两者都取自**专辑**，同一专辑的曲目共享同一个值——与 `language` 一样，不要当成逐曲独立样本。

`basicInfo.genre`/`genreNew`/`genres` 在这份记录里**全是空串**（70/70），`albumTag3` 恒为 `0`、`recLabels` 为 `null`：平台的结构在、内容没给，所以没有 `genre` 这个 extra。专辑简介 `basicInfo.desc` 也不取（长文本，且属于专辑不属于曲目）。

### 歌单级专属字段：创建时间

`cdlist[0].ctime`（秒 epoch）→ `playlist.extra.created_at`，fixture 实测 `2019-05-28T01:45:35Z`（raw `1559007935`）。

不取：`visitnum`（146604）、`cmtnum`、`buynum` 是社交计数；`mtime` 与 `song_update_time` 平台给的就是 `0`（写进去等于给一个假时间）；`tags[]` 有 3 个槽位但 `name` 全是空串。

### 元信息从 `cdlist[0]` 取

| 输出 | 来源 |
| --- | --- |
| `playlist.id` | `disstid`，缺失退 `ref.key` |
| `playlist.title` | `dissname` |
| `playlist.creator` | `nickname` / `uin`（`uin` 可能是脱敏字符串，fixture 实测为 `oi4AoKv5NK4s7n**`，代码原样写入，不做还原） |
| `playlist.description` | `desc` |
| `playlist.declared_count` | `total_song_num`，再退 `songnum`。**两者都缺席时保持 `null`，不退 `len(songlist)`**（旧代码有这层兜底，已删） |

`cdlist[0]` 里还有 `nick`、`encrypt_uin`、`songids` 等键，适配器不使用。

---

## 数量对不上的情况

| 现象 | 代码行为 |
| --- | --- |
| `total_song_num` 与 `songnum` 都缺席 | `declared` 与 `gap` 都保持 `null`，`base.make()` 追加「平台没有给出曲目总数，只取到 N 首：这份数据无法证明完整（ledger.declared 为 null）」，退出码 1。`songlist` 照常解析，只是**无法证明完整** |
| `total_song_num` ≠ `len(songlist)` | warning `歌单声明 {declared} 首，接口返回 songlist {len(songs)} 首`；`declared` 取 `total_song_num`，`ledger.gap > 0`，退出码 1。fixture 实测 `total_song_num=76`、`songnum=76`、`len(songlist)=76`，因此**本次没有这条 warning** |
| 某些专辑详情没解析出来 | warning `{n} 张专辑未取到详情，相关曲目的语种/发行日期为 unavailable`；`album_misses` 按批次里应有的 albummid 数累加 |
| 一个批次都没解析出来 | warning「取到了专辑详情请求但一张都没解析出来：请求体可能无法离线复算，相关语种/发行日期只能标为 unavailable（这是数据缺陷，不是平台没有）」——这条专门区分「平台没给」与「我们没能离线复算」，两者在 `ledger` 里看起来一样，必须靠文案分辨 |
| 曲目缺 `albummid` | `Field.reason` 为「该曲目没有 albummid，拿不到专辑语种（QQ 音乐的语种是专辑级字段）」，`language` 变 `unavailable`，但 `duration_ms` 仍可算 |
| 曲目缺 `interval` | `duration_ms` 为 `unavailable`，原因「该曲目没有 interval」 |

---

## 失效症状

| 症状 | 抛出 | 退出码 |
| --- | --- | --- |
| 歌单响应不是 JSON 对象 | `UpstreamChanged("qq: playlist 接口返回的不是 JSON 对象")` | 3 |
| `cdlist` 为空且 `code ∈ {-1, 10, 1000, None}` | `FetchError(kind=KIND_NOTFOUND, context={key, code, subcode})`，文案「歌单不存在、未公开或需要登录」 | 2 |
| `cdlist` 为空但 `code` 是别的值 | `UpstreamChanged("qq: playlist 返回结构异常 (code=…)")` | 3 |
| 专辑批次返回空对象（多半是请求体被包了一层） | 不抛异常，但触发上面两条「未取到详情」warning；语种/发行日期大面积为 `unavailable` | 0 或 1 |
| 输入里解析不出 id | `InputError("qq: 无法从输入中解析歌单 id: …")` | 4 |

**注意「空成功」这个坑**：`musicu.fcg` 对结构错误的请求体通常回 HTTP 200 + 空对象，不会报错。所以出现「语种覆盖率为 0 但响应字节数正常」时，第一件事是去 `raw/album_detail_000_req.json` 看请求体是不是一层 `req_j` 结构。

---

## 失败路线备忘

| 路线 | 现状 |
| --- | --- |
| `{"data": "<json 字符串>"}` 包裹的 `musicu.fcg` 请求体 | 已废弃：返回空对象，静默丢字段。当前代码只发一层 `req_j` 结构，并把请求体存进 `raw/` 以便复算 |
| `time_public` / `adate` 作为发行时间 | 已废弃：这些字段常为 `None`。发行时间只从专辑详情 `basicInfo.publishDate` 取，取不到就是 `unavailable`（继承观察，未复核：v1 笔记记录） |
| 通过 `songids` 反查曲目顺序 | 不使用。顺序以 `songlist[]` 为准，`songids` 只当背景信息 |
| 一次请求取全部专辑 | 已废弃：`musicu.fcg` 按 30 个 `req_j` 一批，超批会失败或截断 |

---

## 维护提示

```
mpa doctor --provider qq --live --id 7011264340    # 联网自检
mpa fetch https://y.qq.com/n/ryqq/playlist/7011264340 --out /tmp/qq
mpa verify /tmp/qq
mpa export /tmp/qq --format csv | head -3           # 确认 language 仍是专辑级的值
```

`mpa doctor --provider qq --live --id 7011264340` 报出 `language` 或 `released_at` 覆盖率骤降、或 `warnings` 里出现「一张都没解析出来」＝**`musicu.fcg` 的请求体结构或专辑详情响应结构变了，重读本文件的「请求头/前置条件」与「数量对不上的情况」两节**。
