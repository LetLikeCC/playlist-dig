# 数据契约：`mpa/playlist@1`

这份文档写给「上游接口变了、要动手改适配器」的人。它描述的是**代码现在的实际行为**，不是设计意图：每一条都可以用仓库里的 `tests/fixtures/` 离线复核，不需要联网。

复核方式：

```
mpa verify tests/fixtures/<provider>          # 用 raw/ 重新推导并与 playlist.json 逐字节比对
mpa export tests/fixtures/<provider> --format csv   # 看每个字段实际取到了什么
mpa adapters                                  # 各适配器的模式、能力位、最后实测日期
```

约定：本文出现的具体数字都来自 `tests/fixtures/`（抓取时间见各 `fetch.log.json` 的 `fetched_at`），凡是没有 fixture 支撑的说法都明确标注「未复核」。代码与文档冲突时以 `mpa/` 下的代码为准。

---

## 1. 顶层结构

`Playlist.to_obj()` 的键**顺序固定**，`mpa/export.py:canonical_json()` 不做排序，所以这个顺序也是磁盘上的字节顺序：

| 顺序 | 键 | 类型 | 说明 |
| --- | --- | --- | --- |
| 1 | `schema` | string | 恒为 `mpa/playlist@1`（`mpa/model.py` 的 `SCHEMA_PLAYLIST`） |
| 2 | `source` | object | 这份数据是怎么取来的，见第 5 节 |
| 3 | `playlist` | object | 歌单元信息，见第 3.3 节 |
| 4 | `ledger` | object | 数量账本，见第 4 节 |
| 5 | `capabilities` | object | 该平台能不能填某个字段（布尔），见第 6 节 |
| 6 | `warnings` | array | 字符串数组，可以为空 |
| 7 | `tracks` | array | 曲目数组，见第 3 节 |

注意两件事：

- 顶层**没有** `meta` 键，也**没有** `errors` 键。歌单元信息在 `playlist` 下；抓取期的错误记录在数据集目录的 `fetch.log.json` 里（见第 7 节），不写进 `playlist.json`。
- 空值会被 `mpa/model.py:_clean()` 丢掉：`None`、`[]`、`{}`、`""` 都不写盘（数字 `0` 与 `false` 保留）。因此读数据时不能假设每个键都存在，必须当成可选键处理。

---

## 2. `Field` 对象

`TRACK_FIELDS` 里的每个字段都是一个 `Field`，而不是裸值。这样每个值都能回答「它从哪来、为什么没有、是否只是猜的」。

| 键 | 类型 | 何时出现 | 说明 |
| --- | --- | --- | --- |
| `value` | 任意 | 总是 | 归一化后的值；`unavailable` 时为 `null` |
| `source` | string | 总是 | `platform` / `derived` / `unavailable` 三者之一 |
| `reason` | string | `source=unavailable` 时必有 | 人可读的原因，直接进报告 |
| `method` | string | `source=derived` 时必有 | 生成方法名，例如 `script-of-title` |
| `confidence` | string | `source=derived` 时必有 | 目前只出现 `low` |
| `raw` | 任意 | 仅在原始值与归一化值**不同**时 | 上游原值，用于人工比对与将来重算 |

序列化顺序就是上表顺序（`Field.to_obj()`），空键同样被 `_clean()` 丢掉。

### 2.1 三条不可违反的规则

1. **`derived` 必须带 `method` 和 `confidence`。** `Field.derived()` 的签名把 `method` 设成必填参数，`confidence` 默认 `low`。适配器的 derive 路径里不允许出现「有值但没说怎么来的」字段。
2. **没有平台值就不要编。** 平台给了值就是 `platform`；只有能由平台值按明确规则算出来的才是 `derived`（且必须带 `method`/`confidence`），其余一律 `unavailable` 并写原因。**不做语种猜测**——网易云没有语种字段，那 200 条就诚实地是 `unavailable`。
3. **取不到就是取不到。** `Field.platform(value)` 收到 `None` 或 `""` 时会自动转成 `Field.unavailable(reason or "provider returned no value")`；适配器不会在抓取路径里填空值或猜测值。`unavailable` 的曲目仍然是数据的一部分，会计入 `ledger.unavailable`，不算抓取失败。

### 2.2 `raw` 为什么时有时无

`Field.platform(value, raw=...)` 只在 `raw != value` 时保留 `raw`。这不是遗漏，而是为了避免冗余。`tests/fixtures/` 里的真实例子：

| 提供商 | 字段 | 磁盘上的 `Field` | 原因 |
| --- | --- | --- | --- |
| netease | `released_at` | `{"value":"2022-08-19","source":"platform","raw":1660924800000}` | 原值是毫秒时间戳，归一化后不同 |
| kugou | `collected_at` | `{"value":"2026-02-13","source":"platform","raw":{"collecttime":1770985183}}` | 原值是 unix 秒 |
| kugou | `language` | `{"value":"国语","source":"platform"}` | 原值就是 `"国语"`，`raw` 被丢弃 |
| kugou | `released_at` | `{"value":"2026-02-05","source":"platform"}` | `publish_date` 已是 `YYYY-MM-DD` |
| qishui | `duration_ms` | `{"value":142620,"source":"platform","raw":{"ms":142620}}` | 原值与值相等，但这里显式传了字典，字典内容不等于整数，故保留 |

因此「没有 `raw`」**不等于**「没有原始值」，只说明原始值已经等于归一化值。判断来源要看 `source`，不要看 `raw` 是否存在。

### 2.3 反序列化

`Field.from_obj(None)` → `Field.unavailable("absent from stored data")`；非字典值按裸平台值处理（`Field(value=obj, source="platform")`）。读旧数据集时字段缺失不会抛异常，只会变成一条带原因的 `unavailable`。

---

## 3. `tracks[]`

### 3.1 曲目形状

`Track.to_obj()` 的键顺序：

| 顺序 | 键 | 类型 | 说明 |
| --- | --- | --- | --- |
| 1 | `index` | int | 从 0 开始，最终输出顺序 |
| 2 | `status` | string | `ok` 或 `unavailable` |
| 3 | `reason` | string | 仅 `status=unavailable` 时出现 |
| 4 | `id` | string | 平台侧曲目标识，没有就是缺键 |
| 5 | `title` | string | 曲名 |
| 6 | `artists` | array | `{name, id}` 列表；空列表会被丢掉 |
| 7 | `album` | object | `{name, id, release_date}`；全空时被丢掉 |
| 8 | `duration_ms` | `Field` | 毫秒 |
| 9 | `language` | `Field` | 平台自己的语种字符串 |
| 10 | `released_at` | `Field` | 归一化为 `YYYY-MM-DD` |
| 11 | `collected_at` | `Field` | 归一化为 `YYYY-MM-DD` |
| 12 | `extra` | object | 仅在该平台有额外字段时出现（见 3.2） |

`status=unavailable` 的曲目保留 `index`/`id`/`reason`，字段值按 `unavailable` 写入，`title` 可能为 `null`：它占用歌单里的一个位置，不能悄悄丢掉，否则 `index` 会与平台侧错位。

### 3.2 `TRACK_FIELDS` 与 `extra`

`TRACK_FIELDS = ("duration_ms", "language", "released_at", "collected_at")`（`mpa/model.py`）。这四个都是**标量** `Field`，`mpa/export.py:provenance()` 与 `mpa/coverage.py:coverage()` 都只遍历这四个。列表型字段（`artists`）和对象型字段（`album`）不在 `TRACK_FIELDS` 里，因此不受 `Field` 契约与覆盖率统计约束。

`extra` 是平台专属字段的口袋，里面的值同样是 `Field`：

| 提供商 | `extra` 键 | fixture 中的观察 |
| --- | --- | --- |
| netease | `popularity` | 200/200 全为 `100.0`，见第 11 节 |
| netease | `alias` | 39/200。`song.alias` 是平台给的**别名数组**（如《海屿你》→`["求你别离开我"]`），整段留成列表，不拼接、不拆词 |
| netease | `trans_name` | 17/200。`song.transName`（如「You know that I feel bad」） |
| qq | `album_type` | 76/76（专辑级）：`Single` 61、`录音室专辑` 10、`EP` 5，来自 `basicInfo.albumType` |
| qq | `label` | 75/76（专辑级）：`company.name`，`独立发行` 71、其余 4 个各 1 |
| kugou | `fileid` | 50/50，min 847 / max 896 / median 871.5 |
| kugou | `remark` | 50/50。平台写在曲目上的说明串（`《星曜计划》音乐企划` 9、`《繁星互娱》厂牌` 4……），38 种取值 |
| kugou | `bpm` | 50/50，平台自己算的速度值，min 56 / max 177 / median 87 |
| qishui | `genre` | 65/65。`track.tags[]` 里 `category.tag_name == "Genre"` 的 `first_level_tag.tag_name` 原样透传（`Pop` 49、`Hip Hop/Rap` 9、`Chinese Style` 3、`Others` 2、`Folk` 2），`raw={"tag_id": …}` |
| qishui | `subgenre` | 56/65。同上的 `second_level_tag.tag_name`，平台没给二级就**不出这个键**（`Chinese Pop` 48、`DJ` 2、`GuFeng Music` 2、`Chinese Folk` 2、`Dance Pop` 1、`Indie Pop` 1） |
| qishui | `composers` | 63/65。`track.song_maker_team.composers[].name`，平台署名列表（`乐火能量` 10 次最多）；它记的是**平台这次写的署名**，不是「作者是谁」的权威答案 |
| qishui | `lyricists` | 63/65。同上取 `lyricists[].name` |

`capabilities` 会同步多出同名布尔位（netease 有 `popularity`/`alias`/`trans_name`，qq 有 `album_type`/`label`，kugou 有 `fileid`/`remark`/`bpm`，qishui 有 `genre`/`subgenre`/`composers`/`lyricists`），这样调用方不必猜哪些平台有额外字段。每个 `extra` 键在 fixture 上的覆盖率由 `tools/gen_readme_numbers.py` 生成进 README（`extra` 表，分母是该平台实取到的曲目数），所以它不会像手写表格那样烂掉。

`extra` 里的字符串是平台值的**原样透传**，只做一件事：去掉首尾空白；去掉之后是空的就**不出这个键**（`_genres()` 与 `_language()` 在这一点上一致，理由是「一串空格」不是平台给的信息，把它当值写进数据等于自己造假数据）。数组型的值（`alias`、`composers`）同样只做这一件事——把每个元素的空白去掉、丢掉空元素，**不拼接、不排序、不去重**，平台给几个就是几个。除此之外不做映射、不做归类、不做单位换算——需要解释才能读懂的东西（映射表、归类、阈值）不属于抓取结果（哪些字段被这样挡在外面、理由是什么，见第 11 节）。

**注意 `extra` 是「这次抓取时平台写的是什么」，不是曲目的固有属性。** 汽水同一个歌单两次抓取，同一首歌的 `tags[].first_level_tag` 从 `Hip Hop/Rap` 摇到 `Pop`、`second_level_tag` 摇成 `null`（见 [references/qishui.md](./references/qishui.md) 的「已知不稳定字段」），所以 `extra.genre`/`extra.subgenre` 是 qishui 唯一会跨抓取变动的派生字段；`contract_diff.py` 对 `extra` 只看「键是否整批消失」，不看覆盖率比例，正是因为这个抖动是平台侧行为而不是回归。

### 3.3 `playlist`（歌单元信息）

`PlaylistMeta.to_obj()` 的键顺序：`id`、`title`、`creator{name,id}`、`description`、`declared_count`、`extra`。`creator` 在 `name` 与 `id` 都为空时整个键被丢掉（四个 fixture 里 `name`/`id` 都有值，因此都带着 `creator`）；`extra` 为空时同样不出现，所以老数据集读起来完全不变。

**歌单级 `extra`（第 3.3.1 节）与曲目级 `extra`（第 3.2 节）是两回事**：前者描述这张歌单本身（标签、创建/更新时间），后者描述每一首歌。混在一个口袋里会让人以为「这个标签每首歌都有」，所以位置分开，`capabilities` 里的键名也分开。

#### 3.3.1 `playlist.extra`

| 提供商 | 键 | fixture 中的观察 |
| --- | --- | --- |
| netease | `created_at` / `updated_at` / `track_updated_at` | 平台给毫秒 epoch（`createTime`/`updateTime`/`trackUpdateTime`），值是 UTC ISO，`raw` 留原数：`2013-09-09T10:10:06Z` / `2026-09-29T00:22:25Z` / `2026-09-29T12:32:09Z` |
| qq | `created_at` | `ctime`，秒 epoch `1559007935` → `2019-05-28T01:45:35Z` |
| kugou | `tags` | `musiclib_tags[].tag_name` → `["流行", "网络", "甜蜜", "精选"]`，`raw={"tag_ids": [9, 35, 47, 1084]}`。同一条 data 里还有个逗号串 `tags`（内容相同），取结构化的这一份，避免同一信息两个来源 |
| kugou | `created_at` / `updated_at` | `create_time`/`update_time`（秒）→ `2026-01-29T15:14:20Z` / `2026-02-13T12:19:44Z` |
| kugou | `published_at` | `publish_date` 平台本来就写成 `YYYY-MM-DD`，原样透传（`raw` 因相等而被丢掉） |
| qishui | `created_at` / `updated_at` | `create_time`/`update_time`（秒）→ `2024-08-02T04:02:53Z` / `2026-09-29T15:44:42Z` |
| kuwo | — | **一个都没有**：这条链路没有 fixture，没验证过的字段不写进 capabilities（见第 11 节末） |

四条规矩：

1. **只收「歌单自身的属性」**：标签、创建/更新时间、发布时间。社交计数（netease 的 `playCount`/`subscribedCount`/`commentCount`/`shareCount`、qq 的 `visitnum`/`cmtnum`/`buynum`、kugou 的 `collect_total`/`heat`、qishui 的 `stats`）**一律不取**——它们每次请求都在动，进了数据只会让两次抓取的结果白白抖动，而且最容易被当成「热度排名」的权威数字。理由逐条写在第 11 节。
2. **时间只做一件事：epoch → UTC ISO**，`raw` 留平台原数（netease 毫秒、其余秒）。epoch 是绝对的，这个换算没有时区猜测；与 `released_at`/`duration_ms` 同一条约定：换算单位不改 `source`（仍是 `platform`）。
3. **`capabilities` 用前缀区分作用域**：曲目级 extra 直接写键名（`genre`），歌单级写成 `playlist.tags` 这样带前缀的形式。带前缀的键必须能在 `tests/_support.py` 的 `playlist_extra_keys` 里找到，`tests/test_adapters_offline.py::TestExtrasAreHonest` 双向检查「不许吹牛、不许漏报」。
4. **平台没给就是 `unavailable` + `reason`**，不是空串也不是 0；整个歌单级 `extra` 都没值时，`playlist.extra` 这个键不出现。

歌单级字段与曲目级字段**作用域不同**：导出时它们各占一组列（`playlist_*` 前缀），不会混进逐曲列里——「描述歌单的字段」和「描述每首歌的字段」不能混着数。

---

## 4. `ledger`：数量账本

写盘顺序：`declared`、`fetched`、`ok`、`unavailable`、`duplicates_removed`、`reasons`、`gap`（`gap` 固定在最后）。

| 键 | 类型 | 含义 |
| --- | --- | --- |
| `declared` | int 或 **null** | 平台声明的曲目总数。**这个键永远存在**：平台没给就是 `null` |
| `fetched` | int | 实际进入 `tracks[]` 的条数（含 `unavailable`） |
| `ok` | int | `status=ok` 的条数 |
| `unavailable` | int | `status=unavailable` 的条数，例如已下架曲目留下的空位 |
| `duplicates_removed` | int，省略即 0 | 分页去重移除的条数；为 0 时被 `_clean` 丢弃 |
| `gap` | int 或 **null** | **计算字段**：`declared is None ? None : max(0, declared - fetched)`。**这个键也永远存在** |
| `reasons` | object，省略即空 | `{不可用原因: 条数}`，按 `Track.reason` 原样计数 |

`declared` 与 `gap` 是唯一两个「即使为 `null` 也照写」的键（`Ledger.to_obj()` 显式拼回去，绕开 `_clean`）。原因是 `null` 在这里是**信息**而不是缺值：它表示「平台没说总数，因此没人能证明这份数据是完整的」。把这一对键清掉就把「不知道」写成了「没问题」。

规则：

- `gap` 只衡量「声明了但没取到」，不衡量「取到了但已下架」；后者记在 `unavailable` 与 `reasons` 里，并在 `reasons` 里写清原因（`unavailable` 是数据的一部分，不是抓取失败）。
- `declared` 为 `null` 时 `gap` 也是 `null`，此时 `Ledger.complete` 恒为 `False`（无法判断是否完整），`mpa fetch` 的退出码按 `_exit_for()` 落到 **1**。
- `declared is None` 时适配器必须追加一条 warning（`mpa/adapters/base.py::make()` 统一做，适配器自己不用写）：`平台没有给出曲目总数，只取到 {n} 首：这份数据无法证明完整（ledger.declared 为 null）`。**不许用 `len(取到的)` 顶替 `declared`**——那样「声明数 = 取到数、gap = 0」就成了伪造的完整性证明；netease 的 `trackCount`、QQ 的 `total_song_num`/`songnum` 缺失时现在都保持 `None`。
- `Ledger.complete` 的定义（只影响库内部判断，不写盘）：`declared is not None and ok + unavailable >= declared and unavailable == 0`。
- `unavailable: 0` 和 `gap: 0` **会**写盘（数字 0 不被 `_clean` 丢弃），所以「键存在且为 0」是正常状态；不要用 `if ledger.get("gap")` 区分「没有缺口」和「没有信息」，要用 `is None`。

fixture 实例：netease `{"declared":200,"fetched":200,"ok":200,"unavailable":0,"gap":0}`（无 `duplicates_removed`，无 `reasons`）。四个 fixture 都有真实声明数，因此 `gap` 都是 `0`，没有一个是 `null`——`null` 分支目前只有单元测试覆盖。

---

## 5. `source`：这份数据怎么来的

`SourceInfo.to_obj()` 的键顺序与出现条件：

| 顺序 | 键 | 类型 | 说明 |
| --- | --- | --- | --- |
| 1 | `provider` | string | 适配器名：`netease` / `qq` / `kuwo` / `kugou` / `qishui` |
| 2 | `input` | string | 用户原始输入（URL 或 id）。**这里不是** `ref`；平台原生 id 存在 `fetch.log.json` 的 `ref.key` 里 |
| 3 | `adapter_version` | string | 生成该数据的适配器版本，目前 `2.0.0` |
| 4 | `upstream_commit` | string | **这次运行适配器认定的上游版本**（`ctx.upstream_commit`）：工具自己下载并拉起 node 服务时是实际装的那个 commit；用 `--service-url` 指向外部服务时，服务报不出自己的版本，适配器回落到它钉住的常量。直连 HTTP（netease/qq）与汽水 SSR 路线**没有这个键**（值为 `None`，被 `_clean` 丢掉） |
| 5 | `fetched_at` | string | UTC，`%Y-%m-%dT%H:%M:%SZ` |
| 6 | `mode` | string | 见 5.1 |
| 7 | `raw_saved` | bool | 是否保存了 `raw/` |
| 8 | `raw_digest` | string | 全部原始响应的 sha256 摘要 |
| 9 | `requests` | int | HTTP 请求次数 |

### 5.1 `mode` 取值

| 值 | 含义 |
| --- | --- |
| `direct-http` | 直接请求平台接口（netease、qq） |
| `ssr-partial` | 抓分享页 SSR 数据（qishui 默认路线，未升级到 node 服务时） |
| `node-service-local` | 由本工具下载并启动的本地 node 服务（kugou 默认路线） |
| `node-service-external` | 用户用 `--service-url` 指定的外部服务（kugou fixture 即此值） |

注意 `node-service-local` / `node-service-external` 是适配器从 `ServiceHandle.mode` 直接赋值给 `ctx.mode` 的结果，路径中**不**带 `node-service:` 前缀；触发 node 服务的 qishui 数据同样写 `node-service-external`，光看 `mode` 无法区分是哪个平台的哪条路线，要结合 `provider` 与 `fetch.log.json` 的 `ctx.extra.service`。

**`upstream_commit` 有两个含义，别把两处当同一个数。** `playlist.json.source.upstream_commit` 是**这次运行适配器认定的**版本（= `ctx.upstream_commit`，见上表第 4 行）；`fetch.log.json.upstream_commit` 是**这份代码钉住的目标**，写的是 `ctx.upstream_commit or adapter.upstream_commit`（`pipeline.py:136`）——`ctx` 里没有时它照样会有值，因为适配器类上带着常量。四个 fixture 正好把三种情形摆齐：

| fixture | `playlist.json.source.upstream_commit` | `fetch.log.json.upstream_commit` | `fetch.log.json.ctx.upstream_commit` |
| --- | --- | --- | --- |
| netease / qq（`direct-http`） | 无此键 | `null` | `null` |
| kugou（`node-service-external`） | `9b6a779…` | `9b6a779…` | `9b6a779…` |
| qishui（`ssr-partial`） | 无此键 | `e409c10f…` | `null` |

也就是说：**两处相等只说明这次运行和代码常量一致，不等于服务就是那个版本**（外部 `--service-url` 的情形）；**两处不等也不代表数据有问题**——qishui 那行就是「日志记下了适配器钉住的服务版本，而这次根本没走服务」。`tools/rederive_fixture.py` 与此无关：它只重算 `playlist.json`。酷狗那条路线的细节见 [references/kugou.md](./references/kugou.md) 的「外部服务：`--service-url`」一节。

### 5.2 `adapter_version` / `raw_saved` / `raw_digest` / `requests` 是抓取期事实

这四个字段描述的是**那一次抓取**，不是从响应体推导出来的，因此：

- 它们的值在 `mpa fetch` 结束时由 `mpa/pipeline.py` 从适配器版本、`RawStore` 与 `HttpClient.log` 写入；
- `mpa verify` 重新推导后，会用 `fetch.log.json` 里的 `adapter_version`、`raw_saved`、`raw_digest`、`len(requests)` **还原**这四个键再比对（`pipeline.verify_playlist()` 里显式赋值），否则「旧版本适配器抓的数据」在离线复算时永远不可能相等；
- `adapter_version` 另有独立判定：`adapter_version_ok`，默认只 `[warn]`，`--strict` 下才计入，见第 8.0 节；
- `raw_digest` 另有独立校验：与磁盘上 `raw/` 现算的摘要比对，见第 8 节。

`raw_digest` 的算法（`mpa/rawstore.py:digest()`）：按 `seq` 排序后，对每个 part 拼接 `"{seq}:{name}:{sha256}"` 再取 sha256。只要有一个响应体被编辑，摘要就变。

---

## 6. `capabilities` 与 `warnings`

`capabilities` 是 `{字段名: bool}`：该平台**理论上**能不能填这个字段。它不因某一首歌缺值而变，缺值的个别情况记在 `Field.reason` 与 `warnings` 里。四个标准键是 `duration_ms`、`language`、`released_at`、`collected_at`，此外可以有平台专属键：曲目级 extra 直接写键名（netease `popularity`、kugou `fileid`），歌单级 extra 带 `playlist.` 前缀（`playlist.tags`、`playlist.created_at`），见第 3.2/3.3 节。

`warnings` 是给人类读的字符串数组，规则：

- **必须能从 `raw/` 复算出来**，否则 `mpa verify` 会失败。因此 `warnings` 里只放数据本身的性质，例如 `歌单声明 76 首，接口返回 songlist 76 首`、`分页在上限 15 页处停止，可能仍有未取到的曲目`、`语种为专辑级字段，同一专辑内所有曲目相同：英语 40、纯音乐 24、国语 10、其他 2`。
- `--partial-ok` 只在**运行日志**里留痕，不进 `warnings`：`fetch.log.json` 有 `partial_ok: true`，而 `playlist.json` 不变。原因见 `pipeline.py` 的注释——「调用方接受了缺口」不是数据的性质，复算不出来。

---

## 7. 数据集目录与序列化

```
<outdir>/
  playlist.json           mpa/playlist@1 契约数据（canonical_json）
  fetch.log.json          mpa/fetchlog@1，抓取过程记录
  raw/_manifest.json      {requests:[...], count, bytes, digest}
  raw/<name>.json|txt     原始响应体（.txt 逐字节；.json 解析后重新紧凑序列化，内容等价、非字节等价）
  provenance.json         仅 tests/fixtures/ 有，手工维护，工具从不写
```

`playlist.json` 与 `fetch.log.json` 都用 `canonical_json()`：`json.dumps(..., ensure_ascii=False, indent=2, sort_keys=False) + "\n"`。`sort_keys=False` 是刻意的：dataclass 字段顺序即契约顺序，人读 diff 时能看到「谁在哪个位置变了」。`raw/` 里的 json 反而用 `separators=(",", ":")` 紧凑写盘，因为它是给机器复算的原文。

`raw/_manifest.json` 每个条目（`RawStore.record()`）：`seq`、`name`、`method`、`url`、`status`、`kind`（`json`/`text`）、`bytes`、`sha256`、`saved_at`。QQ 的专辑详情请求把**请求体**也存成一个 part（`album_detail_000_req`，`status` 为 `null`），因为响应里的键是位置化的 `req_0..req_n`，没有请求体就无法解释哪个响应属于哪张专辑。`fetch.log.json` 里的 `requests[]` 更轻：`name`、`url`、`method`、`status`、`bytes`、`ms`（不含请求体）。

**两处 `bytes` 含义不同，不要互相印证。** manifest 里的 `bytes` 是**落盘文件**的大小，而文件是 `json.dumps(payload, ensure_ascii=False, separators=(",", ":"))` 重新序列化的紧凑形式；`fetch.log.json.requests[].bytes` 是 `runtime.py` 记的**线上收到的字节数**（`len(resp.read())`）。同一份响应这两个数通常不同：QQ fixture 的 4 个响应里有 3 个不同（`album_detail_000` 54335 对 54260、`album_detail_001` 55516 对 55506、`album_detail_002` 17591 对 17586），`playlist_fcg` 恰好相等（57618，推测那份响应本来就是这种紧凑形态，未复核）。要判断「上游响应变大变小」看 `fetch.log.json`；要判断「磁盘上这份 raw 多大」看 manifest。

`fetch.log.json` 顶层键（`pipeline.fetch_playlist()`）：

| 键 | 说明 |
| --- | --- |
| `schema` | 日志自身的契约名 |
| `provider` / `input` / `ref` | 用了哪个适配器、原始输入、解析出来的 `{provider,key,params}` |
| `mode` / `adapter_version` / `upstream_commit` | 路线与版本留痕。这里的 `upstream_commit` 是**代码钉住的目标**（`ctx.upstream_commit or adapter.upstream_commit`），与 `playlist.json.source.upstream_commit`（本次实际用的）含义不同，见 5.1 节末 |
| `started_at` / `finished_at` / `elapsed_s` | 抓取时间 |
| `raw_saved` / `raw_digest` | 是否存了 `raw/`、以及这次抓取的摘要 |
| `requests` | 轻量请求列表：`name`、`url`、`method`、`status`、`bytes`、`ms`（不含响应体） |
| `errors` | 抓取过程中的错误记录 |
| `ctx` | `input`、`ref{provider,key,params}`、`mode`、`upstream_commit`、`fetched_at`、`extra` —— **离线复算只依赖它**，所以适配器的 derive 不允许读时钟、文件系统或网络 |
| `partial_ok` | 调用方是否用了 `--partial-ok`（只影响提示，不影响退出码，见第 4 节） |
| `exit_code` | 本次抓取的退出码 |
| `warnings` | 适配器产出的告警列表 |
| `raw_cleanup` | **仅非空时出现**。往同一个输出目录二次抓取时，上一次留下的响应文件会被 `RawStore.finish()` 删掉，被删名单记在这里 |
| `extra` | **仅非空时出现**。`ctx.extra`：`--partial-ok` 时含 `{"partial_ok": true}`；服务路线下含 `service{base_url,mode,install_method,resolved_digest,upstream_commit}` |

`raw_cleanup` 的存在条件很重要：`raw/` 必须**恰好**是产出这份 `playlist.json` 的那批响应。如果只往 `raw/` 里加不删，混进上一代适配器的旧文件后，没人能说清哪几个文件算出了当前结果，`mpa verify` 的 `manifest_ok` 也会因此失败（见第 8 节）。因此二次抓取到同一目录是**自愈**操作：`mpa fetch <input> --out <同一个目录>` 会清掉多余文件并把名字写进 `raw_cleanup`。

一个容易踩的点：`Adapter._check_parts_match()` 分两步断言「记的 = 用的」——先是**名字集合**相等（交给 `derive` 的 part 名与写进 `raw/` 的完全一致），再是每个 part 的**值**相等（`store._parts[name] != value` 就报错；JSON part 比的是解析后的对象，不是磁盘字节）。在线抓取时不一致会立刻抛 `MPError`，提示哪一侧多了哪个名字。看到这条报错就说明适配器 collect/derive 的分工写错了，`mpa fetch` 修 bug 时必须过这一关。

---

## 8. `mpa verify` 到底检查什么

```
mpa verify <dir> [--strict] [--json]
```

五项检查，**默认其中四项参与判定**，参与判定的检查任一条不过即退出码 **2**：

| 检查项 | 通过条件 | 是否参与默认判定 |
| --- | --- | --- |
| `schema_ok` | `playlist.json` 的 `schema == "mpa/playlist@1"` | 是 |
| `manifest_ok` | `raw/` 里每个响应重新算 sha256 与 `raw/_manifest.json` 相符；manifest 记录的 part 一个不缺；**没有** manifest 未记录的 `*.json`/`*.txt`（隐藏文件不算）；且 manifest 的 `digest` 与各项 sha256 一致 | 是 |
| `raw_digest_ok` | `playlist.json.source.raw_digest` 与 `_manifest.json` 的 `digest` 一致（`raw_digest` 为 `None` 时跳过） | 是 |
| `adapter_version_ok` | 数据里的 `adapter_version` 与当前代码一致 | 否，仅 `--strict` |
| `derive_deterministic` | 断网重跑适配器 `derive`，结果与 `playlist.json` **逐字节**相同 | 是 |

判定用的名单由代码显式给出（`required`），不是「所有检查项都要过」：

- 默认：`required = ["schema_ok", "manifest_ok", "raw_digest_ok", "derive_deterministic"]`，`ok = all(checks[name] for name in required)`。
- `--strict`：在 `required` 末尾追加 `adapter_version_ok`。

因此 `adapter_version_ok` 失败在默认跑时只是**警告**：CLI 打印 `[warn] adapter_version_ok`（而不是 `[FAIL]`），`ok` 仍为真、退出码仍为 0。代码注释把这条取舍写得很直白：verify 要回答的是「数据变了吗」，不是「工具版本变了吗」。

命名上注意 `_manifest.json` **带下划线前缀**，不是 `manifest.json`。

`--json` 的输出结构固定为：

```json
{"ok": true, "checks": {"schema_ok": true, "...": true}, "required": ["schema_ok", "manifest_ok", "raw_digest_ok", "derive_deterministic"], "diffs": []}
```

`checks` 里五项都在（包括不参与判定的 `adapter_version_ok`），`required` 才是决定 `ok` 的名单；判定失败时 `diffs` 给出差异文本。想看「是不是当前这版适配器抓的」必须自己看 `checks.adapter_version_ok`，或者加 `--strict`。

失败时打印首个差异的**行号与精确字符**（`_first_diff()` 故意不截断成前缀，避免「两行看起来一样」的假相等），命令退出码 **2**。

`mpa verify` 会退 **3** 的情形只有一种：复算时适配器的 `derive` 主动抛 `UpstreamChanged`（`raw/` 还在、但那份响应的结构已经不满足当前契约里的断言，例如某个本该是对象的字段变成了字符串）。这时 `verify` 报的是「这份记录的字段形态已经不认识了」，而不是「数据被改过」——后者一律是 2。

### 8.0 抓取过程的四个字段会被还原

`adapter_version`、`raw_saved`、`raw_digest`、`requests` 描述的是**抓取过程**，不是推导结果。verify 在比对前先用 `pipeline.restore_fetch_meta(playlist, log)` 从 `fetch.log.json` 把这四个值恢复进复算结果（`tools/rederive_fixture.py` 用的是同一个函数，所以重算工具与 verify 不会各自漂移），所以：

| 场景 | 默认 | `--strict` |
| --- | --- | --- |
| 数据确实由旧版本适配器抓的（`playlist.json` 与 `fetch.log.json` 里的 `adapter_version` 都是旧值，当前代码是新的） | 通过，只打印 `[warn] adapter_version_ok`，退出码 0 | 失败（`adapter_version_ok` 进 `required`），退出码 2 |
| 只手改 `playlist.json` 的 `adapter_version`（日志里还是新值） | **失败**：`adapter_version_ok` 仍是 `[warn]`，但 `derive_deterministic` 会 FAIL，因为复算时从日志还原出的版本与文件不一致 | 同样失败 |
| `fetch.log.json` 里的 `requests` 条数被改 | 不影响（从日志还原） | 不影响 |

第二行是这一节最容易看错的推论：`[warn]` 说的是「版本这一项不判定」，不等于「这个字段随便改都没事」。判定是否「同一份数据」的仍然是 `derive_deterministic` 的逐字节比对。

`raw_digest` 是例外中的例外：它同时被 `raw_digest_ok` 独立地与磁盘上的 `_manifest.json` 对账，改日志改不动磁盘。

`raw_digest` 的算法值得单独记住，因为它决定「改了 `raw/` 的排列顺序算不算改动」：按请求发生顺序（`seq`）依次拼 `seq:name:sha256` 再取 sha256，**不是**按名字排序。所以它是「这一次抓取」的指纹：同一份响应换个抓取顺序，摘要会变；而 `mpa verify` 复算时也用同一个函数，两边一致才能过。

**前置条件：`raw/` 必须存在且非空。** 缺失或为空（`mpa fetch --no-raw` 取的目录）时 verify 抛 `InputError`，退出码 **4**（输入问题），不是 2；`mpa export` 对这类目录仍然可用，退出码 0。

### 8.1 可以自己复现的反例

这是「`raw/` 留存」这条设计标准的可验证证据，改坏了立刻能看见：

| 操作 | 结果 |
| --- | --- |
| 篡改 `raw/<已记录的>.json` 的内容 | `manifest_ok` 失败（sha256 不符），退出码 2 |
| 往 `raw/` 里多放一个 `*.json`/`*.txt` | `manifest_ok` 失败（manifest 未记录的文件，例如上一次抓取中途失败留下的半份响应），退出码 2；往同一目录重跑 `mpa fetch` 会自愈，被清掉的文件名记在 `fetch.log.json` 的 `raw_cleanup`（第 7 节） |
| 只改 `playlist.json` 里的字段值 | `derive_deterministic` 失败（与复算结果不再逐字节相同），退出码 2 |
| 把 `playlist.json` 的 `schema` 改成别的值 | `schema_ok` 失败，退出码 2 |
| 抹掉 `playlist.json` 里的 `raw_digest` | `raw_digest_ok` 跳过（`None` 视为「当初没记」），但 `derive_deterministic` 失败：复算会从日志把 `raw_digest` 还原回来，与文件不再逐字节相同，退出码 2 |
| 用旧版本适配器抓的数据（版本号在 `playlist.json` 与 `fetch.log.json` 里一致） | 默认通过（只 `[warn]`），`--strict` 失败（见 8.0） |
| 删掉 `raw/` 里某个 part，或整个 `raw/_manifest.json` | 复算**之前**就报错：抛 `DatasetError`，一行中文说明（`raw/<name>.* 缺失（manifest 里有记录）` / `_manifest.json 不存在…`）加一句「重跑一次 `mpa fetch` 到同一个目录即可自愈」，退出码 2。缺件不会被当成「零个 part」 |
| `raw/` 里某个字段的形态不再符合契约断言（例如 `track.tags[].category` 变成了字符串） | `derive` 抛 `UpstreamChanged`，退出码 **3**：这份记录已经不是当前代码认识的样子了，猜一个值比报错更糟 |

「只改 `playlist.json` 字段值」那一行尤其说明问题：能篡改 `playlist.json` 却改不动结论，正是因为结论由 `raw/` 决定。

verify 的边界，说清楚以免误解：

- 它**不**联网、不启动 node、不读时钟，因此不会发现「上游又变了」；上游变了要靠重新 `mpa fetch`、`tools/contract_diff.py`（第 8.3 节）或 `mpa doctor --live` 才能发现。
- 它**不**重新检查 URL 是否正确、请求头是否仍被接受：这些信息只在 `fetch.log.json` 里留痕。
- 它对 `raw_saved`/`raw_digest`/`requests`/`adapter_version` 采取「从日志还原再比对」的策略（第 8.0 节），所以改了 `fetch.log.json` 里的 `requests` 条数不会导致失败，但磁盘上的 `raw/` 被改动会。
- 它**不**容忍数据集缺件：`raw/` 少一个 part、或少了 `_manifest.json`，都在复算之前抛 `DatasetError`，用一行错误说清缺了什么、怎么自愈（退出码 2）——不会退化成一个 `KeyError` 栈，也不会被当成「这次没有 part」。

### 8.2 改了 `derive` 的解释逻辑，怎么重算 fixture

`mpa/adapters/*.py` 里只改「怎么读已经存在的响应」（漏了一个字段、单位写错、键名记错），而 `raw/` 一个字节没变时，不要重新联网抓：

```
python3 tools/rederive_fixture.py tests/fixtures/qishui      # 单个
python3 tools/rederive_fixture.py tests/fixtures/* --json     # 全部，机器可读
```

它做的事：`RawStore(write=False)` 读回 `raw/` 里的记录 → 跑真实适配器的 `derive` → 用 `restore_fetch_meta()` 还原四项抓取期字段 → 与磁盘上的 `playlist.json` 做 `canonical_json` 比对，只有真的变了才改写 → 立刻 `verify_playlist(strict=True)`。退出码 0 表示「重算并验证通过」，1 表示重算后 verify 不过。没变时打印「重算结果与磁盘一致，未改写」。

**红线：这个工具只用来让「解释逻辑」与已有 `raw/` 重新对齐，不是让红灯变绿的手段。** 上游响应本身变了（字段没了、多了、单位换了）就必须重新 `mpa fetch`，让新的 `raw/` 成为新记录——用重算工具去盖住一个形状已经变了的上游，等于把证据删掉再宣布通过。

### 8.3 跨时间发现上游漂移：`tools/contract_diff.py`

`mpa verify` 只守「这份 raw 能不能复算出这份 `playlist.json`」，因此它有一个结构性盲区：**对刚抓下来的数据跑 verify 几乎永远通过**。上游悄悄不再返回某个字段、不再给总数、少发一次请求，verify 都看不出来；唯一会响的信号是适配器主动抛 `UpstreamChanged`（退出码 3，要求代码里已经写死了那条断言）。

补这个盲区的是结构比较器，它只比**形状**，不比取值：

```
python3 tools/contract_diff.py tests/fixtures/netease /tmp/fresh-netease
python3 tools/contract_diff.py tests/fixtures/netease /tmp/fresh-netease --json
```

判定为漂移（退出码 1）的条件：

| 形状变化 | 例子 |
| --- | --- |
| 曲目里原本存在的键整批消失 | `released_at` 从所有曲目上消失 |
| `extra` 里的平台附加字段整批消失 | netease 的 `popularity`；汽水的 `genre`/`subgenre`（判据只看「还在不在」，比覆盖率抖动严的口径会天天误报） |
| `TRACK_FIELDS` 里的字段 `platform` 覆盖率塌陷（fixture ≥50%，这次不到它的一半） | `language` 从 100% 掉到 0% |
| `ledger.declared` 从数字变 `null` | 平台不再给总数，等于无法证明完整 |
| 请求计划里少了 part | 少发了一次 `song_detail` 请求 |

只提醒不判红的 `[注意]`：路线 `mode` 变了、多了 part、覆盖率小幅变化、这次有 `gap > 0`、这次没有声明总数。`[信息]`：新增曲目键或 `extra` 键、覆盖率上涨。

值不在范围内：歌单加歌、时长差一秒、点赞数变化、汽水 SSR 页面每次不同都不该让 CI 红（见本节末与 [references/qishui.md](./references/qishui.md)）。退出码：0 = 形状一致（可能带 `[注意]`），1 = 漂移，2 = 用法或读取错误。`canary` 工作流在 `mpa verify` 之后对每个平台多跑这一行，`drift=1` 同样开 issue。

实测（本次复核）：`contract_diff.py tests/fixtures/netease tests/fixtures/netease` 退出 0；把 netease fixture 深拷贝后删掉每首的 `released_at`/`extra`、把 `ledger.declared` 改成 `null`、从 `fetch.log.json` 里去掉 `song_detail_000`，五条漂移全部报出、退出 1。

**禁止用 `raw_digest` 或整份 `raw/` 做跨时间比较。** 摘要只是「那一次抓取」的指纹，平台响应里任何无关字段动一下它就变：汽水页面实测抓了两次同一个歌单（间隔几小时），65 首的 id 与顺序、标题、创建者、语种全部逐字节相同，`raw_digest` 却从 `43791ffa…` 变成 `98c08679…`（细节见 [references/qishui.md](./references/qishui.md) 的「已知不稳定字段」）。反过来它也给不出「上游没变」的结论：`raw_digest` 相同只说明那份响应逐字节一样（`derive` 是确定性的，派生结果自然也一样），而「平台今天还返回不返回这些字段」这个问题只有 `contract_diff.py` 能答。

---

## 9. 错误类型与退出码

错误种类在 `mpa/errors.py`：`input`、`network`、`http`、`blocked`、`notfound`、`private`、`parse`、`upstream_changed`、`truncated`、`route_unavailable`。其中只有 `network` 与 `http` 标记为可重试（`RETRYABLE`）。

**规则：库代码从不调用 `sys.exit()`。** 库只抛 `MPError` 子类（`InputError`、`FetchError`、`UpstreamChanged`、`RouteUnavailable`）；只有 `mpa/cli.py` 把异常翻译成退出码。因此同一个函数既能被 CLI 用，也能被别的程序 import。

| 退出码 | 名字 | 触发条件 | 由谁产生 |
| --- | --- | --- | --- |
| 0 | `ok` | `fetch`：`ledger.declared` 有值且 `ledger.gap == 0`、未抛异常；`verify`：`required` 里的检查全过（默认四项，`[warn]` 不算失败）；`export`/`adapters`：正常读完数据；`doctor`：无 FAIL 行 | `pipeline._exit_for()` / `_verify()` 等 |
| 1 | `partial` | `fetch`：**`ledger.declared is None`**（平台没说总数，无法证明完整）或 `ledger.gap > 0`（声明了但没取到）。`_exit_for()` 先判 `declared is None`，两种情形都返回 1。**`--partial-ok` 不会把退出码改成 0**，它的作用是「路线不可用（例如汽水服务起不来）时退回部分数据，而不是整条命令失败」；缺口照旧让退出码为 1，只额外打印一行 `--partial-ok：接受不完整结果（缺口 N 首 / 平台没有给出总数），退出码仍为 1；缺口照旧写在 ledger 和 warnings 里` | `pipeline._exit_for()` |
| 2 | `failed` | 抓取失败（网络/被拦截/解析失败），或 `mpa verify` 的 `required` 检查任一不过（`--strict` 时含 `adapter_version_ok`），或 `mpa doctor` 有 FAIL 行（含环境/连通性失败） | `errors.exit_code_for()` / `_verify()` / `_doctor()` |
| 3 | `upstream_changed` | 抛出了 `UpstreamChanged`：产出了数据但契约检查不通过（结构漂移）。`mpa fetch` 的 `collect` 与 `mpa verify` 复算时的 `derive` 都会走到这里 | `errors.exit_code_for()` |
| 4 | `usage` | 输入/参数不对（例如裸数字 id 无法判断平台、`--provider` 名字不认识、目录不是有效输出目录、用 `--no-raw` 取的目录没有 `raw/` 可复算） | `errors.exit_code_for()` |
| 5 | `route_unavailable` | 这条路线当前不可用（分享页失效、本地 node 服务起不来、缺 node/npm） | `errors.exit_code_for()` |

补充：

- **退出码 2 不只表示抓取失败**，它同时覆盖两类非抓取失败：`mpa verify` 的检查失败，以及 `mpa doctor` 的环境检查（例如缓存目录不可写，可用 `PLAYLIST_DIG_HOME` 覆盖该目录）与 `--live` 字段验证失败（`run_doctor()` 里任何一行的 `ok` 为假且未被标记为 `skipped`，`report["_exit"]` 就置 2）。所以把 `mpa doctor` 当脚本闸门用时，环境问题会正确地让它非 0，而不是只打印一行 FAIL。
- **`skipped` 行不影响退出码。** 最典型的例子是酷我：「没有确认可用的公开样例 id」（`SAMPLES["kuwo"] = None`）按 skipped 处理，`--live` 全绿时为 0。用「不拿猜的 id 去验证」换取一条确定的绿，这是刻意的取舍。
- `KeyboardInterrupt` → 130；`BrokenPipeError` → 0。CLI 打印错误时格式为 `错误：{message}`，再有 `{key}: {value}` 形式的 `context`，可重试的错误追加 `（这类失败通常可以重试）`。

---

## 10. 字段可用性矩阵

「平台侧有/无」指 `capabilities`，括号内是 `tests/fixtures/` 实测的覆盖率与单位处理。**平台专属字段不在这一节**：曲目级的 `extra` 见第 3.2 节、歌单级的 `playlist.extra` 见第 3.3.1 节，逐键覆盖率由 `tools/gen_readme_numbers.py` 生成进 README（手写表格会烂，生成的不会）。

| 字段 | netease | qq | kuwo | kugou | qishui |
| --- | --- | --- | --- | --- | --- |
| `duration_ms` | 有（200/200；`duration` 毫秒直接用） | 有（76/76；`interval` 秒 ×1000，`raw={"sec":…}`） | 有（`duration` 秒 ×1000，`raw={"sec":…}`） | 有（50/50；`timelen` 毫秒，缺失时退到 `duration`，`raw` 同时带两者） | 有（65/65；`duration` 毫秒，`raw={"ms":…}`） |
| `language` | **无**（`unavailable` 200/200；平台不返回语种） | 有（76/76，但**专辑级**：同一专辑同值，`raw={"granularity":"album"}`） | 有（曲目级 `language`） | 有（50/50，曲目级：国语 48 / 粤语 2） | 有（65/65，曲目级；平台给的是**代码**：`ZH` 64 条、`EN` 1 条，`raw={"lang_codes":[…]}`）。取数路径**不做**代码→显示名映射，`language` 保持代码原样；本仓库任何路径都**不做**代码→显示名映射，也不做跨平台归一 |
| `released_at` | 有（200/200；`album.publishTime` 优先，缺失退到曲目级 `publishTime`，再退到 `unavailable`） | 有（76/76；专辑 `publishDate`，`raw` 里标了 `granularity: album`） | 有（曲目 `releaseDate`） | 有（50/50；`publish_date`） | 有（65/65；专辑 `release_date` unix 秒） |
| `collected_at` | **无** | **无** | **无** | 有（50/50；`collecttime` unix 秒；fixture 里 50 条全是 `2026-02-13`） | **无** |

单位与归一化统一约定：

| 上游形态 | 处理 | 代码位置 |
| --- | --- | --- |
| 毫秒整数 | 直接用 | netease `duration`、kugou `timelen`、qishui `duration` |
| 秒整数 | ×1000，`raw` 保留 `{"sec": n}` | qq `interval`、kuwo `duration` |
| 毫秒时间戳 | `iso_date()` → `YYYY-MM-DD`，`raw` 保留原值 | netease `publishTime`（例如 1660924800000 → `2022-08-19`） |
| unix 秒时间戳 | `iso_date()` → `YYYY-MM-DD`，`raw` 保留原值 | kugou `collecttime`、qishui `release_date` |
| `YYYY年MM月DD日` | `iso_date()` 解析 | `mpa/timeutil.py` 的 `_CN` |
| 已经是 `YYYY-MM-DD` | 原样，`raw` 因等于值而被丢弃 | kugou `publish_date` |

`mpa/timeutil.py` 的阈值：大于等于 `10**11` 视为毫秒，大于等于 `10**8` 视为秒，否则按日期字符串解析。**注意 `duration_ms` 与时间戳走的是不同路径**：时长只做单位换算，不做时间戳推断，避免把「4 分钟」当成 1970 年。

---

## 11. 已知的未实现字段

这一节只关心一件事：**原文里有、我们明确不取的字段，以及为什么不取**。先说已经修好的两类：`qishui.lang_codes`（`language` 现在 65/65，平台代码原样透传）
与曲风 `tags`（`extra.genre` 65/65、`extra.subgenre` 56/65）——它们都是「平台给了、我们扔了」，
属于最该修的一类。数字全部来自 `tests/fixtures/*/raw/` 里的真实响应，离线可复算；
被取的那些字段可以用 `mpa export <dir> --format csv` 或 README 的生成表复核。

这一节的规矩是：**只列「原文里有、适配器明确不取」的字段**，并写清为什么不取。曾经出现在这里的 `qishui.lang_codes` 已经修好（`language` 现在 65/65，平台代码原样透传），曲风 `tags` 也一并在同一轮修好（`extra.genre` 65/65、`extra.subgenre` 56/65）——这两处都是「平台给了、我们扔了」，属于最该修的一类。

仍未实现的（数字全部来自 `tests/fixtures/*/raw/` 里的真实响应，离线可复算；被取的那些字段可以用 `mpa export --format csv` 或 README 的生成表复核）：

| 平台 | 原文里的字段 | fixture 里的情况 | 为什么不取 |
| --- | --- | --- | --- |
| qishui | `track.stats`（`count_collected`/`count_comment`/`count_shared`） | 65/65 | 是**平台侧的计数**，每次请求都在动（同一歌单两次抓取 `count_collected` 从 3209832 变成 3209847），拿它当热度是 netease `popularity` 那类饱和值陷阱 |
| qishui | `playlistInfo.resource_cnt.track_cnt`、`stats.count_visible` | 70（对 `count_tracks` 的 65） | 与真声明数冲突，拿它当 `declared` 会凭空造出 5 首的假缺口；已由坑 1 记录 |
| qishui | `track.relation_media`（如「电影《后来的我们》片尾曲」） | 只有 3/65 有 | 同一首歌两次抓取平台给的值都不同（「电视剧《真情》」↔「电影《后来的我们》」），当元数据用会误导 |
| qishui | `track.label_info.is_original` | 41/65 **全为 `true`**，缺失的 24 条**不是 `false`** | 缺键与「不是原唱」在原文里分不清，当布尔字段用会把两种含义混成一种；要用得先确认平台语义（未复核）。注意 `Field.platform(False)` 本身是合法的（`model.py` 只把 `None`/`""` 判成 missing），所以这不是模型限制，是语义未定 |
| qishui | `track.label_info.only_vip_playable` / `only_vip_download` / `quality_map` | 33/65 为 `true` | 讲的是**播放与下载权限**，不是曲目本身的属性；歌单数据里带上它只会让消费者以为是内容字段 |
| qishui | `track.vocal` | 65/65 恒为 `1` | 常数（本歌单里没有任何信息量）：进 `extra` 也只是多一个恒等值 |
| qishui | `track.vid`、`track.album.…` 里未取的部分 | 65/65 | `vid` 是视频标识，不参与歌单分析；`album` 只取了 `name`/`id`/`release_date` |
| qishui | `track.preview` / `chorus` / `first_vocal` / `nodes` / `bit_rates` | 58~65/65 | 讲的是**试听片段与音频码率**（`preview.duration`、副歌起止毫秒、逐档码率），属于播放而不是歌单内容 |
| netease | `playlist.playCount` / `subscribedCount` / `commentCount` / `shareCount` | 14159011840 / 13181843 / 319034 / 67374 | 社交计数：抓的是某一刻的快照，每次都变；当「热度」用会变成第二个 `popularity` 陷阱 |
| netease | `playlist.tags` / `displayTags` / `distributeTags` / `algTags` | 全是空数组或 `null` | 平台这次没给标签，不是我们不取——空数组写成 `tags` 会被误读成「标签为空」 |
| netease | `song.playedNum` / `dayPlays` / `starredNum` / `score` / `fee` / `mvid` / `single` | 200/200，多为 `0` 或常量 | 计数（全 0）、付费类型码（`fee=8` 要一张表才读得懂）、MV 标识：要么需要解释，要么在本歌单里没有信息量 |
| qq | `playlist.visitnum` / `cmtnum` / `buynum` / `mtime` / `song_update_time` | `mtime` 与 `song_update_time` 平台给的就是 `0` | 前三个是社交计数；后两个这次是 0，写进去等于给一个假时间 |
| qq | `playlist.tags[]` | 3 个槽位，`name` 全是空串 | 平台给了结构但没给内容，`name: ""` 不是标签 |
| qq | `song.size128/size320/sizeflac/…`、`song.pay` | 76/76 | 文件体积与付费方式：属于播放与版权，不是曲目属性 |
| qq | `albumInfo.desc`（专辑简介） | 70 张里 20 张有，最长几百字 | 长文本，且是**专辑**简介不是曲目标签；要分析文案的人可以直接从 `raw/` 里取 |
| kugou | `playlist.collect_total` / `heat`、`song.heat` | `collect_total`/`heat` 在这份记录里是 `0` | 社交计数，且这次全 0：写进去只会让人以为「热度为 0」 |
| kugou | `song.tagmap.genre0` | 50/50，取值 `524288`/`526336`/`0`… | **位掩码**：`524288` 对应哪个曲风需要平台侧的一张映射表，我们不许自己造这张表 |
| kugou | `song.extname` / `bitrate` / `level` / `relate_goods` / `download` / `feetype` / `privilege` | 50/50 | 音频格式、码率档位与下载权限：播放属性，不是曲目属性 |
| kugou | `song.audio_id` / `album_id` / `mixsongid` | 50/50 | 平台内部主键；标识已经在 `Track.id`（`hash`）与 `album.id` 里，多存一份只会让「哪个才是 id」变模糊 |
| netease / qq / kugou / kuwo | 各自响应里**未逐个核对**的其它字段 | 未统计 | 见各平台 reference 的「字段与坑」；这一节的规矩是「没看过」和「看过不取」要分开写，不要混着说 |
| kuwo | 全部字段 | 无 fixture | 整个平台没有第一手数据，见 [sources.md](./sources.md) 第 4 节。**因此 `capabilities` 里也没有任何 `playlist.*` 或 extra 键**：没验证过就不声明 |

**判别标准**：一个字段该不该进 `extra`，看它是不是「平台当次给的值」**且不需要我们的解释就能读懂**。是就取（`Field` 带 `source`/`raw`，消费者自己决定信不信）；需要我们的映射表、归类、阈值，或者像 `is_original` 那样「缺键与假值分不清」，就不取——那需要一层分析才能下结论，不能混进抓取结果。**这一条不能拿来当丢弃数据的借口**：`lang_codes` 与曲风两个字段曾经被扔在这里，它们既不缺语义也不需要映射，属于「平台给了、我们扔了」，是 bug 不是设计。

---


## 维护提示

字段语义或契约改动后，按顺序重跑：

```
python3 tools/rederive_fixture.py tests/fixtures/*   # 只改了 derive 的解释逻辑时，先重算并验证
mpa verify tests/fixtures/netease                    # 每个有 fixture 的平台都跑一遍
mpa export tests/fixtures/netease --format csv       # 新字段有没有真的出现在导出里
mpa adapters                                         # 确认 capabilities / reference 路径 / 实测日期
python3 tools/gen_readme_numbers.py --check          # README 的覆盖表有没有过期（CI 也跑）
python3 tools/check_doc_refs.py                      # 文档提到的命令/文件/章节号是否都存在（CI 也跑）
```

`mpa verify` 报出「重新推导的结果与 playlist.json 不一致」＝**契约或派生逻辑变了，先看本文档第 2、3、8 节，再改适配器**。

新增一个平台适配器时的硬规矩：**`declared` 必须来自平台自己给的总数**（`trackCount`、`total_song_num` 这类字段），平台上没有这个数字就让它保持 `None`，由 `base.make()` 追加 warning、由 `_exit_for()` 把退出码变成 1。**绝不能用 `len(取到的曲目)` 顶替**——那样「声明数 = 取到数、`gap = 0`」看起来完美，实际上把静默截断洗成了完整数据，正是本项目最反对的失败方式。这条也适用于老适配器：netease 与 QQ 里原来的 `or len(...)` 兜底已经按这个规矩删掉。

判断「上游是否变了」要分两层，别拿错工具：

| 问题 | 用什么 | 说明 |
| --- | --- | --- |
| 这份入库数据还能不能从它自己的 `raw/` 复算出来 | `mpa verify`（第 8 节） | 只证明「这份 raw 能推出这份 playlist.json」，不管上游现状 |
| 今天的上游跟入库 fixture 相比，**形状**是否还一样 | `python3 tools/contract_diff.py <fixture 目录> <刚抓的目录>` | 只比形状不比取值，见第 8.3 节；canary 用它在 `mpa verify` 之后多守一道 |
| 连形状都变了、需要人工看现场 | `mpa doctor --provider <x> --live --id <id>` | 抓一份新的并报告字段缺失 |

`raw_digest` **不能**用来跨时间比较：它只是「这一次抓取」的指纹，平台每次响应里的无关字段（汽水 SSR 页面的 `node_uuid`、点赞数、标签）动一下就会变，见 [references/qishui.md](./references/qishui.md) 的「已知不稳定字段」。

各平台请求头、接口与坑见 [references/](./references/)。
