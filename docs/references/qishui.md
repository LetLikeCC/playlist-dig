# 汽水音乐（qishui）

| 项 | 值 |
| --- | --- |
| 适配器 | `mpa/adapters/qishui.py`；`name="qishui"`，`label="汽水音乐"`，`version="2.0.0"` |
| 输入形态 | `https://music.douyin.com/qishui/share/playlist?playlist_id=<id>`；裸数字 id；`qishui.douyin.com/s/<token>` 短链 |
| 路线 | 先 `direct-http`（分享页 SSR，`default_mode="ssr-partial"`）；声明数大于 SSR 实际给到的数时才升级到 node 服务（`guowenye/qishui-api@e409c10`） |
| 最后验证 | 2026-09-29：「分享页 playlist_id=7397692920558452788：声明 65 / 实取 65，SSR 一路即可，未触发 node 服务」 |
| 上游接口 | `GET https://music.douyin.com/qishui/share/playlist?playlist_id=<id>`（mobile UA）；服务路线 `GET <base>/playlist/detail?playlist_id=<id>` |

复核状态：本文的字段映射、两个计数、`lang_codes`、`tags`（曲风）等均用 `tests/fixtures/qishui/` 重新核对（`mpa verify tests/fixtures/qishui --strict` 五项全过，`raw` 1 项 = `ssr_page.txt`，529603 B）。三处「平台给了、我们却丢掉」的解析缺陷已在代码里修好并离线重算 fixture（`raw/`、`fetch.log.json` 未动）：`description`/`creator`（坑 2）、`language`（坑 3）、曲风 `genre`/`subgenre`（坑 4）。同一歌单当天再抓一次，除 `extra` 里的曲风外派生结果逐字节相同、`raw/` 却不同，见「已知不稳定字段」一节。**SSR 约 175 首上限、以及 v1「390 首被当成 175 首」的例子，继承自 v1 的观察，本次未复核**（本次 65 首，没触发这条边界）。

字段契约、`Field` 语义、退出码见 [../schema.md](../schema.md)。

---

## 请求头/前置条件

| 项 | 值 |
| --- | --- |
| SSR 请求 | `GET https://music.douyin.com/qishui/share/playlist?playlist_id={key}`，part 名 `ssr_page`，按文本存（`ssr_page.txt`） |
| 请求头 | 只加 `User-Agent: MOBILE_UA`（iPhone Safari）。不需要 cookie、不需要登录、不签名 |
| 短链 | `qishui.douyin.com/s/<token>` 用 urllib 跟随跳转取最终 URL，再从中抠 `playlist_id`；只允许 `qishui.douyin.com` 与 `music.douyin.com`（SSRF 防护），其他域名 `InputError` |
| 服务路线 | `GET {base}/playlist/detail?playlist_id=<urlencoded>`，`Accept: application/json`，`error_hints={404: "该路由不存在"}` |
| 服务管理 | 与酷狗同一套 `NodeService`：钉 commit、下 tar.gz、`pnpm install --frozen-lockfile` 或 `npm install`、绑 `127.0.0.1` 随机端口；也可用 `--service-url` 走外部服务 |

### 什么时候才会起 node 服务

只有一条判据：`declared`（SSR 页面 `playlistInfo.count_tracks`）**大于** SSR 实际嵌入的 `medias` 条数。`declared <= len(medias)` 时**不会**碰 node，`source.mode` 保持 `ssr-partial`，`source.upstream_commit` 也**缺席**（`base.make()` 只写 `ctx.upstream_commit`，SSR 路线不设置它）。

升级失败时（缺 node、服务起不来）：

- 默认：把 `RouteUnavailable` 包一层重新抛出，文案带「歌单声明 N 首，SSR 只提供 M 首，需要 qishui-api 服务才能拿全」，退出码 5。
- 加 `--partial-ok`：不抛，把失败原因记进 `ctx.extra["service_error"]`，derive 时变成 warning「未能使用 qishui-api 服务：…」，退出码按 `ledger.gap` 落到 1。

也就是说 `--partial-ok` 在这里的作用是「宁可拿到 SSR 的那部分，也不要整条命令失败」，而不是「忽略缺口」。

### 解析 SSR：`_ROUTER_DATA` 是主名，`OUTER_DATA` 是兜底

`parse_ssr()` 做的事是：定位 SSR 载荷的起始标记（`_find_marker()`）→ 从其后第一个 `{` 开始 `json.JSONDecoder().raw_decode()` → 取 `loaderData.playlist_page` → 返回 `(playlistInfo, medias)`。

标记由 `SSR_MARKERS = ("_ROUTER_DATA", "OUTER_DATA")` 给出：`_find_marker(html)` 把每个标记的 `html.find()` 位置收集起来，**取最靠前的那个命中**，全都找不到才返回 `-1`。

**过去这里只 `html.find("OUTER_DATA")`，能命中其实是个巧合**：真实页面里的变量是 `_ROUTER_DATA`，`OUTER_DATA` 只是它的后半段子串。现在两个名字都试，而且因为 `_ROUTER_DATA` 的位置比它内部的 `OUTER_DATA` 早一位，实际锚点落在真实变量名上；万一上游改回光秃秃的 `OUTER_DATA`，旧名仍然兜得住。解析结果与之前完全一致（命中同一段 JSON），所以这不是行为变更，只是把「靠子串巧合」换成「两个名字都摆出来」。定位这类故障时仍建议直接 `grep -o '_ROUTER_DATA'` 页面确认（本次复核时该字符串出现在页面偏移 87611 处），不要只看错误文案。

两处 `UpstreamChanged` 文案统一成 `qishui: SSR 页面里找不到 _ROUTER_DATA / OUTER_DATA - 结构可能已变化` 与 `qishui: 原始响应里找不到 _ROUTER_DATA / OUTER_DATA - 无法复算`。

---

## 字段与坑

| 输出 | 来源 | 备注 |
| --- | --- | --- |
| `track.id` | `track.id` | |
| `track.title` | `track.name` | 空/缺失的位是下架占位，见下 |
| `track.artists` | `track.artists[].name` 与 `[].id`（`id` 是 platform 值，`raw` 里不带）；一个都没有时占位 `<汽水未返回歌手>` | 占位名会被原样写进数据，这是刻意的可见缺陷 |
| `track.album` | `track.album` 的 `name`/`id`，`release_date` 取 `album.release_date` | |
| `duration_ms` | `track.duration`（**毫秒**），`raw={"ms": …}` | |
| `released_at` | `album.release_date`（Unix **秒**）→ `YYYY-MM-DD`，`raw` 保留原整数 | fixture 实测 `raw: 1716940800` → `"2024-05-29"` |
| `language` | `track.lang_codes`（**代码**，原样透传） | **不是每首都有**：fixture 那份 65/65，但 2026-10-03 实测另一份歌单只有 28/59（原始 SSR 里另外 31 首确实没有 `lang_codes`，不是解析漏了），缺的写成 `unavailable` + 原因。fixture 实测 65/65：`ZH` 64 条、`EN` 1 条（`index = 6`，id `6704982295578675202`，《We Don't Talk Anymore》）。多语种用 `+` 连接，原始数组在 `raw={"lang_codes":[…]}`；取数路径**不做**代码→显示名映射（见坑 3）。**本仓库任何路径都不做代码→显示名的映射**（那是我们的解释，不是平台值） |
| `collected_at` | 无，恒 `unavailable`，原因「汽水接口不返回收藏时间」 | |
| `extra.genre` | `track.tags[]` 里 `category.tag_name == "Genre"` 的 `first_level_tag.tag_name` | 65/65（`Pop` 49、`Hip Hop/Rap` 9、`Chinese Style` 3、`Others` 2、`Folk` 2），`raw={"tag_id": …}` |
| `extra.subgenre` | 同上的 `second_level_tag.tag_name`，没有二级就不出这个键 | 56/65（`Chinese Pop` 48、`DJ` 2、`GuFeng Music` 2、`Chinese Folk` 2、`Dance Pop` 1、`Indie Pop` 1）。**这个字段平台侧会摇摆**，见「已知不稳定字段」 |
| `extra.composers` / `extra.lyricists` | `track.song_maker_team.composers[].name` / `.lyricists[].name` | 各 63/65（2 首平台没给署名）。值是按平台顺序的**名字列表**，不排序不去重；`facts` 摊平后给 `distinct`/`top`（作曲 `乐火能量` 10 次最多）。它记的是**平台这次写的署名**，不是作者归属的权威答案 |
| `playlist.id` | `playlistInfo.id`，退 `ref.key` | |
| `playlist.title` | `playlistInfo.title` | |
| `playlist.creator` | `owner.nickname` / `owner.id`；取不到时退 `user_artist_info.user_brief`（`nickname`/`name` 与 `id`/`uid`），两个都取不到就整键省略 | 静态辅助 `QishuiAdapter._creator(info)`，`_from_ssr` 与 `_from_service` 共用 |
| `playlist.description` | `desc`，退 `description` | 静态辅助 `QishuiAdapter._description(info)`；SSR 与服务的响应键名不同，所以两个都试 |
| `declared_count` | `playlistInfo.count_tracks` | |
| `playlist.extra.created_at` / `updated_at` | `create_time` / `update_time`（秒 epoch）→ `2024-08-02T04:02:53Z` / `2026-09-29T15:44:42Z`，`raw` 留原数 | 不取 `stats`（收藏/评论/分享数，每次都变）与 `resource_cnt`（与真声明数冲突，见坑 1） |

坑：

1. **`declared` 只认 `count_tracks`，而同一份 payload 里还有别的数。** fixture 实测 `count_tracks = 65`、`medias` 65 条，但 `resource_cnt.track_cnt = 70`、`stats.count_visible = 70`，标题里还写着「精选70首」。ledger 按 65 算，`gap = 0`，`warnings` 为空。看到标题数字和 `declared_count` 不一致时先看 `count_tracks`，不要以为漏抓了 5 首。
2. **（已修）`description` 与创建者曾经恒缺席。** 旧代码读的是 `info.get("description")`，而 SSR payload 里字段叫 `desc`；创建者明明在 `owner` 里（`owner = {"id": "2331429247649368", "nickname": "汽水音乐APP", …}`），旧代码却写死 `creator_name=None, creator_id=None`，于是 `playlist` 只剩 `{"id","title","declared_count"}`。现在由 `_creator()` / `_description()` 两个静态辅助函数统一取：创建者取 `owner.nickname` / `owner.id`（`owner` 缺席时退 `user_artist_info.user_brief` 的 `nickname`/`name` 与 `id`/`uid`），简介取 `desc` 再退 `description`；SSR 与 node 服务两条路线共用这两个函数。fixture 已按新逻辑离线重算（`playlist.creator = {"name":"汽水音乐APP","id":"2331429247649368"}`，`playlist.description` 非空，`raw/` 与 `fetch.log.json` 未变），`mpa verify --strict` 五项全过。服务路线那几个键名（`user_brief` 之类）**来自代码分支、未用真实服务响应复核**。
3. **（已修）`lang_codes` 有值却没被用。** fixture 里 65 条中 64 条 `lang_codes = ["ZH"]`、1 条 `["EN"]`，旧代码却把 `language` 写死成 `unavailable`、`capabilities["language"] = false`，于是平台明明给了的语种一个都没进数据。现在由 `_language()` 取 `track.lang_codes`：**原样透传平台代码**（`"ZH"`、多语种 `"ZH+EN"`），`raw={"lang_codes":[…]}` 留原数组，缺字段才 `unavailable`（原因「该曲目没有 lang_codes」），`capabilities` 改为 `true`。取数路径刻意**不做** `ZH → 国语` 这类映射：那是本工具的语言表，不是平台值，混进来以后没人分得清哪个是数据、哪个是我们的解释。
4. **（已修）曲风标签整段被丢。** `track.tags[]` 里 `category.tag_name == "Genre"` 的标签平台 65/65 都有（**fixture 里 131 个标签全部是 `Genre` 类别，没有别的类别**），旧代码没解析，于是「曲风」这个歌单分析最常用的维度是空的。现在 `extra["genre"]` 取一级、`extra["subgenre"]` 取二级（没有二级就不出键），`raw={"tag_id": …}` 留 id。同一首歌可能给多个 Genre 标签（粗一个细一个），取到的就是平台标签本身，不做归类；标签名两侧的空白**会**被去掉（和坑 3 里丢掉空白 code 一致），去掉后为空就不出这个键——绝不让一串空格变成「平台值」。`extra` 是平台专属字段的口袋（见 [../schema.md](../schema.md) 第 3.2 节），不进 `TRACK_FIELDS`。**曲风是平台侧会摇摆的值**（同一首歌的标签两次抓取不一样，见「已知不稳定字段」），所以它记的是「抓的时候平台写的是什么」。
5. **`medias[]` 是包了一层的外壳。** 每条 media 的键是 `collect_info`、`entity`、`id`、`index`、`type`，曲目对象在 `media["entity"]["track"]`（键含 `album`、`artists`、`duration`、`id`、`lang_codes`、`name`、`tags`、`vid`）。写成 `media["track"]` 会一条都取不到。
6. **下架位有占位对象但没名字。** `track` 缺失或 `name` 为空 → `status="unavailable"`，原因「该条位已被下架（SSR 只留空占位）」（服务路线是「…（接口返回空 track）」）。占位仍占 `index`，所以 `tracks[]` 长度与 `medias` 长度一致。
7. **`released_at` 的 `raw` 是整数秒，`value` 是字符串日期。** 不要拿 `raw` 直接比字符串。

fixture 实测（65 首）：时长 65/65 `platform`；`released_at` 65/65 `platform`；`language` 65/65 `platform`（`ZH` 64 / `EN` 1）；`collected_at` 0/65；`caveats` 为 `null`；`extras` 为 `{"genre": {"n": 65}, "subgenre": {"n": 56}}`；首曲 `沦陷（官方1.3x）`/`JuggShots`，`duration_ms {"value": 142620, "raw": {"ms": 142620}}`，`language {"value": "ZH", "source": "platform", "raw": {"lang_codes": ["ZH"]}}`。

---

## 数量对不上的情况

| 现象 | 代码行为 |
| --- | --- |
| SSR 给的比声明少 | `ledger` 记 `declared`/`fetched`/`gap`，warning `SSR 页面只嵌入 {n} 首（声明 {declared} 首）`；退出码 1（缺口） |
| 最终仍不足声明数 | warning `歌单声明 {declared} 首，只取到 {n} 首（SSR 页面上限约 175 首）`——只有 SSR 路线才带括号那句 |
| 服务还没取完（`next_cursor` 非空） | warning `服务仍在翻页（next_cursor={…}），结果可能不完整` |
| 服务路线失败且用了 `--partial-ok` | warning `未能使用 qishui-api 服务：{原因}`，数据退回 SSR 的那部分 |
| 下架/空占位 | 计入 `ledger.unavailable`，`ok` 数相应减少 |
| 页面里没有 `count_tracks`（平台不说总数） | `declared` 与 `gap` 都保持 `null`，`base.make()` 追加 warning「平台没有给出曲目总数，只取到 N 首：这份数据无法证明完整（ledger.declared 为 null）」，退出码 1；也不会因为「声明数」去升级 node 服务——判据是 `declared > len(medias)`，`null` 不满足 |

**SSR 上限约 175 首是继承自 v1 的观察，本次未复核。** 它的后果是确定的：超过上限时 `gap > 0`，退出码 1，除非走通服务路线。v1 笔记里还有「一个 390 首的歌单被当成 175 首就是全部」的例子，同样是继承观察，未复核；适配器 docstring 把它写成设计动机。

---

## 已知不稳定字段：同一歌单两次抓取，`raw/` 每次都不一样

2026-09-29 对同一个歌单（`7397692920558452788`）抓了两次：入库 fixture 一次、当天再抓一次。**除了曲风，派生结果是逐字节相同的**——65 首、同样的 id 与顺序、同样的 `title`/`description`/`creator`、同样的 64 `ZH` + 1 `EN`；但两份 `raw/ssr_page.txt` 完全不同：`raw_digest` 从 `43791ffa…` 变成 `98c08679…`，页面长度 529603 B 对 529619 B。

差异都落在适配器**不取**的字段上，只有 `track.tags` 是例外（它我们取，见下表最后两行）：

| 变化 | 例子（fixture → 当天） |
| --- | --- |
| URL 里的 `node_uuid` | `7691015064555849256` → `7691021056413582882` |
| 头像 CDN 域名轮换 | `p26` / `p3` / `p11.douyinpic.com` 三个 URL 顺序互换 |
| 计数漂移 | `stats.count_collected` 3209832 → 3209847；`count_shared`、`count_comment` 同样在动 |
| 曲目标签（→ `extra.genre`/`extra.subgenre`） | `medias[i].entity.track.tags[].first_level_tag` 从 `Hip Hop/Rap` 变成 `Pop`，`second_level_tag` 从对象变成 `null` |
| 单曲的描述性字段自身不稳定 | `medias[64].entity.track.relation_media`：`电影《后来的我们》片尾曲` → `电视剧《真情》片尾曲` |

最后两行值得单独记住。两次抓取里 `medias[64]` 都是同一首歌（id `7168831604675217446`，《后来》），65 首的 id 顺序也完全一致，所以这是**平台侧对同一首歌给的字段自己不稳定**，不是解析错位。别把它当成「抓到了不同的歌」。

曲风因此是**唯一一个会让 qishui 派生产物跨抓取变动的字段**：它记的是「这次抓的时候平台写的是什么」，不是歌的固有属性。所以

- 拿两份不同时间的 `playlist.json` 比曲风没有意义（歌单本身也可能改了），要比就比同一份 `raw/` 里的东西；
- `tools/contract_diff.py` 把 `extra` 键**整批消失**算漂移（规则是「fixture 里有的 `extra` 键这次一首都没有」），而曲风的覆盖率本身会小幅抖动（fixture 里 `subgenre` 56/65），所以只看「还在不在」，不看比例。

结论：

- `raw_digest` 只是**当次快照**的指纹，不能用于跨时间比较，也不能用来判断「上游是否变了」。
- `mpa verify` 只证明「这一份 `raw/` 能复算出这一份 `playlist.json`」，它对「平台今天给的东西还是不是这个形状」一无所知——那要靠 `tools/contract_diff.py`（见 [../schema.md](../schema.md) 第 8.3 节）或契约测试，`contract_diff` 正是为此不看取值、只看形状。
- 以上两次抓取（入库 fixture 一次 + 当天一次）都在本机联网完成；本节里的具体差异值来自那两次抓取的比对记录，**写文档时没有再次联网**。平台为什么这么给，未复核。

---

## 失效症状

| 症状 | 抛出 | 退出码 |
| --- | --- | --- |
| 页面里找不到 `_ROUTER_DATA` 也找不到 `OUTER_DATA`（改名/换渲染方式/被风控页替换） | `UpstreamChanged("qishui: SSR 页面里找不到 _ROUTER_DATA / OUTER_DATA - 结构可能已变化")`，`context` 带分享页 URL | 3 |
| 离线复算时两个标记都找不到 | `UpstreamChanged("qishui: 原始响应里找不到 _ROUTER_DATA / OUTER_DATA - 无法复算")`（`parse_ssr()` 返回 `None` 时由 `_from_ssr()` 抛出） | 3 |
| SSR 能拿到 `playlistInfo` 但没有 `medias` | 不报错；`declared > 0` 且 `fetched = 0` → `gap` 拉满，退出码 1，warning 说明只嵌入 0 首 |
| 服务 `/playlist/detail` 结构变了 | `UpstreamChanged("qishui: service /playlist/detail 返回结构异常")` | 3 |
| 服务路由 404 | `FetchError`，提示「该路由不存在」 | 2 |
| 需要服务但起不来（缺 node、端口不通、45 s 未就绪） | `RouteUnavailable`（`--partial-ok` 时降级为 warning） | 5（或 1） |
| 短链跳转失败 | `FetchError(kind=KIND_NOTFOUND)`「短链没有跳转到歌单页」/ `KIND_NETWORK`「短链解析网络错误」 | 2 |
| 非抖音域名短链 | `InputError`「拒绝请求非抖音域名（SSRF 防护）」 | 4 |
| 输入里抠不出 `playlist_id` | `InputError` | 4 |

排障第一步固定是：把 `raw/ssr_page.txt` 拿出来搜 `_ROUTER_DATA` 与 `playlistInfo`。数据在页面里就能修解析；页面里没有才是上游真变了。

---

## 路线备忘：试过什么、现在走哪条

| 路线 | 现状 |
| --- | --- |
| SSR 翻页/游标参数 | 无效：分享页忽略游标参数，滚动时也不发 XHR，所以 SSR 天然截断。不要试图给分享页加 `cursor`/`offset` 参数 |
| SSR 全量替代 | 不可行，超过上限只能靠 `qishui-api` 服务 |
| 把 `lang_codes` 当 `language` 用 | 已按「原样透传平台代码 + 原数组进 `raw`」实现（坑 3）。**不要再临时 `"".join(lang_codes)`，也不要在取数路径加 `ZH → 国语` 这类映射**——那是我们的解释，不是平台值。本仓库里任何地方都不做这种映射 |
| 把 `tags` 当曲风 | 已实现（坑 4）：只取 `category == "Genre"` 的标签并区分一/二级。**不要自己归类，也不要只按顺序取第一个标签**（同一首歌可能给粗、细两个，且平台侧会摇） |
| `resource_cnt.track_cnt` / `stats.count_visible` 当声明数 | 与 `count_tracks` 不一致（fixture 里 70 vs 65），会凭空造出 5 首的假缺口。ledger 只用 `count_tracks` |
| 从 `desc`/`owner` 之外的位置找简介与创建者 | 不必再找：适配器已经读 `desc` 与 `owner`（坑 2，已修）。数据一直都在那里，缺的是解析 |

---

## 维护提示

```
mpa doctor --provider qishui --live --id 7397692920558452788
mpa fetch https://music.douyin.com/qishui/share/playlist?playlist_id=7397692920558452788 --out /tmp/qs
mpa verify /tmp/qs
python3 tools/contract_diff.py tests/fixtures/qishui /tmp/qs   # 形状是否还跟入库 fixture 一致
mpa export /tmp/qs --format csv | head -3
```

`mpa doctor --provider qishui --live --id 7397692920558452788` 报出「找不到 `_ROUTER_DATA` / `OUTER_DATA`」＝**分享页的变量名或渲染方式变了（先直接 `grep -o '_ROUTER_DATA'` 页面确认），重读本文件的「解析 SSR」与「失效症状」两节**；若报的是 `declared` 与 `medias` 数量突然拉开，则多半是 SSR 上限又变了。

**不要用 `raw_digest` 判断上游有没有变**：这个页面每次请求都不同（同一歌单两次抓取，65 首 id 与顺序、标题、创建者、语种全部逐字节相同，`raw_digest` 却每次都变；唯一会跟着动的是曲风 `extra.genre`/`extra.subgenre`——见「已知不稳定字段」）。要比就比形状，用 `tools/contract_diff.py`，它的判据与用法见 [../schema.md](../schema.md) 第 8.3 节。
