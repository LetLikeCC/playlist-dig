# 文档来源与仍未复核的部分

本文说明各文档里的结论**分别来自哪里**、怎么复现，以及哪些主张至今**没有第一手证据**。目的是让接手的人知道：看到一条不对的说明时，是应该去改文档，还是应该去怀疑上游。

日期基准：2026-09-29。

---

## 1. 证据分级

| 级别 | 含义 | 可信度 |
| --- | --- | --- |
| A | 直接读 `mpa/` 源码得到，附文件路径 | 最高。代码即行为，但注意代码可能本身有 bug |
| B | `tests/fixtures/<provider>/` 里的真实抓取数据，且 `mpa verify tests/fixtures/<provider>` 通过 | 高。字段名、数量、覆盖率都能离线复算 |
| C | 本次会话联网实测（没有进代码、没有进 fixture） | 中。只证明「当时」是这样 |
| D | 本仓库 v1 的观察（`main` 分支上的 `references/*.md` 与 `scripts/*.py`，即 <https://github.com/LetLikeCC/music-playlist-analysis> 的 v1.x 历史；本次会话逐份对照过，正文里写「继承观察，未复核」的都是这类） | 低。**未复核**：v1 的笔记是当时的一次探测，没有 fixture 支撑；地址在，结论要重跑一次才算数 |
| E | 社区服务仓库的 README/代码（`MakcRe/KuGouMusicApi`、`guowenye/qishui-api`） | 中低。只看过接口形状，没有逐行读它的实现 |

**代码与笔记冲突时以代码为准**，冲突处已在对应文档正文里就地写明（D 级条目一律带「继承观察，未复核」的标记）。

---

## 2. 逐文件来源

| 文档 | 主要依据 | 复现/核对方式 |
| --- | --- | --- |
| [schema.md](./schema.md) | A：`mpa/model.py`、`mpa/pipeline.py`、`mpa/cli.py`、`mpa/errors.py`、`mpa/rawstore.py`、`mpa/adapters/base.py`、`mpa/export.py`、`mpa/coverage.py`、`tools/contract_diff.py`、`tools/rederive_fixture.py`、`.github/workflows/canary.yml`；B：四份 fixture 的实际 JSON | 读代码；`mpa verify tests/fixtures/<x>`；`mpa export tests/fixtures/<x> --format csv`；`python3 tools/contract_diff.py <fixture> <新抓目录>` |
| [references/netease.md](./references/netease.md) | B：`tests/fixtures/netease`（`raw_digest 7ced5461838b…`，200 首，3 个 part）；A：`mpa/adapters/netease.py`；D：旧路线与部分边界观察 | `mpa verify tests/fixtures/netease` |
| [references/qqmusic.md](./references/qqmusic.md) | B：`tests/fixtures/qq`（`raw_digest 69f8a4b184a0…`，76 首，manifest 7 项 = 4 次 HTTP 请求 + 3 个请求体 part）；A：`mpa/adapters/qq.py` | `mpa verify tests/fixtures/qq` |
| [references/kugou.md](./references/kugou.md) | B：`tests/fixtures/kugou`（`raw_digest e8924c06650a…`，50 首，2 个 part，`mode=node-service-external`）；A：`mpa/adapters/kugou.py`、`mpa/adapters/node_service.py`；E：`MakcRe/KuGouMusicApi` | `mpa verify tests/fixtures/kugou`；live 需 node |
| [references/qishui.md](./references/qishui.md) | B：`tests/fixtures/qishui`（`raw_digest 43791ffa78e5…`，65 首，1 个 `ssr_page.txt` = 529603 B）；A：`mpa/adapters/qishui.py`（含 `language` 与曲风 `tags` 两处「平台给了却被丢掉」的修复）；D：SSR 约 175 首上限、390 首那个例子 | `mpa verify tests/fixtures/qishui --strict` |
| [references/kuwo.md](./references/kuwo.md) | **只有 A**：`mpa/adapters/kuwo.py`、`mpa/doctor.py`；D：旧路线（SPA 抓取、第三方服务） | 无 fixture，尚无第一手实测 |

四份带 fixture 的适配器当前都通过 `mpa verify`：默认判定的四项（`schema_ok`、`manifest_ok`、`raw_digest_ok`、`derive_deterministic`）全 `ok`，`adapter_version_ok` 也是 `ok`（数据由当前 `2.0.0` 抓取），因此加 `--strict` 同样通过。

---

## 3. 本次会话新确认的第一手事实

| 事实 | 证据级别 | 说明 |
| --- | --- | --- |
| `collection_3_2132041455_1_0` 是一个 `count = 0` 的空歌单 | C | 联网实测得到空但成功的响应（不是报错）。适合用来测试「声明 0 首」的路径；代码与 fixture 里都没有它 |
| 汽水分享页真实变量名是 `_ROUTER_DATA`（出现在页面偏移 87611），`SSR_MARKERS` 里另留旧名 `OUTER_DATA` 兜底 | A + C | 代码读法（`_find_marker()` 取最靠前的命中）+ 实际页面核对；见 [qishui.md](./references/qishui.md) 的「解析 SSR」 |
| 结构比较器 `tools/contract_diff.py` 能发现「自己抓的 verify 永远通过」那类漂移：对 netease fixture 与它自己比退出 0；把 netease fixture 深拷贝后删掉每首的 `released_at`/`extra`、把 `ledger.declared` 改成 `null`、从 `fetch.log.json` 去掉 `song_detail_000`，它报出 5 条漂移并退出 1 | A + C | 逐条实跑；用法与判据见 [schema.md](./schema.md) 第 8.3 节。canary 已把它接在 `mpa verify` 之后（`drift=1` 开 issue）；2026-10-12 该工作流已在真 runner 上跑过三次（正常全绿、报警演练、修复后复跑），四个平台 `drift=0 fail=0` |
| 汽水 65/65 曲目都有 `lang_codes`（64 个 `["ZH"]`、1 个 `["EN"]`），且 65/65 都有 `category.tag_name == "Genre"` 的标签（`genre` 一级 65/65、`subgenre` 二级 56/65，前者 `Pop` 49、`Hip Hop/Rap` 9、`Chinese Style` 3、`Others` 2、`Folk` 2）——而旧代码把 `language` 写死成 `unavailable`、把整段 `tags` 丢掉 | A + B | 直接读 fixture 的 `raw/ssr_page.txt`（离线，未联网）+ 逐条跑 `QishuiAdapter._language()`/`_genres()`；两处缺陷的性质都是「平台给了、我们扔了」，与 `desc`/`owner`（坑 2）同类。`extra` 覆盖表（[coverage.md](./coverage.md)）由 `tools/gen_readme_numbers.py` 一起生成，防止再靠手写 |
| 汽水同一个歌单两次抓取（入库 fixture 一次、当天联网再抓一次）：除曲风外派生结果逐字节相同（65 首、同序、同 id/title/description/creator、同 64 `ZH` + 1 `EN`），`raw_digest` 从 `43791ffa…` 变 `98c08679…`，页面长度 529603 B 对 529619 B | C（联网实测；写文档时未重复抓取） | 页面差异都在不取或本就不稳定的字段上（`node_uuid`、头像 CDN 域名轮换、点赞/评论计数、`medias[64]` 的 `relation_media`），**但 `track.tags` 我们取**，所以 `extra.genre`/`extra.subgenre` 是唯一会跨抓取变动的派生字段（实测同一首歌的标签从 `Hip Hop/Rap` 摇到 `Pop`、`second_level_tag` 摇成 `null`）。结论：`raw_digest` 只能当次快照用，跨时间比形状。见 [qishui.md](./references/qishui.md)「已知不稳定字段」 |
| QQ fixture 的 `raw/` 里原有上一代适配器留下的 `playlist_detail_v3.json`（未写进 `_manifest.json`），该文件已被删除；删除后四个 fixture 全部通过 `mpa verify` | C | 未记录文件留着就会让 `manifest_ok` 失败；对应 schema.md 第 8.1 节的反例。删除前后的 `raw_digest` 未变，且删除后的摘要仍与 `playlist.json` 记录一致 |
| QQ manifest 记 7 项（4 次 HTTP 请求 + 3 个 `album_detail_00N_req` 请求体） | B | 引用 QQ 请求条数时以 manifest 为准 |
| 篡改 `raw/<name>.json`、往 `raw/` 多放 `*.json`/`*.txt`、或只改 `playlist.json` 的字段值，三种改动都会让 `mpa verify` 失败（分别命中 `manifest_ok`、`manifest_ok`、`derive_deterministic`） | A + C | schema.md 第 8.1 节列出的反例；本次逐条实跑过 |
| `mpa doctor --provider kugou`（离线）实测：环境行里「缓存目录可写」为 `[FAIL] - ~/.cache/playlist-dig（可用 PLAYLIST_DIG_HOME 覆盖） - 不可写，请设置 PLAYLIST_DIG_HOME`，整个命令退出码 2 | C | 印证了「doctor 的环境 FAIL 会让退出码非 0」与「`PLAYLIST_DIG_HOME` 可覆盖」两条；本次会话的沙箱就落在这个状态 |
| `mpa verify` 的五项检查与 `required`：四个 fixture 默认与 `--strict` 全绿（各 5 个 `[ok]`，退出码 0） | B + C | 与 `mpa/pipeline.py::verify_playlist()` 当前实现一致 |
| 汽水 fixture 的 `playlist.json` 是按新代码离线重算的：`python3 tools/rederive_fixture.py tests/fixtures/qishui` 报了两次「重算并改写」（`desc`/`owner` 修复之后一次、`language`+曲风修复之后一次），每次只改 `playlist.json`，`raw/` 与 `fetch.log.json` 一个字节未动；另外三个 fixture 重算时都是「重算结果与磁盘一致，未改写」。`mpa verify tests/fixtures/qishui --strict` 五项全过，`python3 tools/gen_readme_numbers.py --check` 与 `python3 tools/make_provenance.py tests/fixtures/qishui --check` 都报一致 | B + C | 重算工具与 verify 共用 `pipeline.restore_fetch_meta()`；工作流与红线见 [schema.md](./schema.md) 第 8.2 节。**离线重算合法，重新联网抓取后冒充同一份数据不合法**：`raw/` 没动，所以复算链路仍然成立 |

---

## 4. 仍未复核（明确标注为不确定）

| 主张 | 现状 | 怎么补 |
| --- | --- | --- |
| **酷我的全部字段与请求行为** | 完全没有第一手数据。适配器是唯一 `verified_on = None` 的；`mpa doctor --provider kuwo --live` 因为「没有确认可用的公开样例 id」被 skip | 拿一个真实的 `h5app.kuwo.cn/m/bodian/collection.html?playlistId=…` id，跑 `mpa doctor --provider kuwo --live --id <id>` + `mpa fetch` + `mpa verify`，然后回填 `kuwo.md` 的「最后验证」、`SAMPLES["kuwo"]` 与覆盖率数字 |
| 汽水 SSR 约 175 首上限、以及「390 首被当成 175 首」的旧例 | 继承自 v1 笔记；本次 65 首未触发 | 找一个声明数 > 175 的歌单跑 `mpa fetch`，看 warning 与 `gap` |
| 汽水服务路线（`qishui-api`）的实际响应结构与 `next_cursor` 行为 | 本次未走服务路线（fixture `mode=ssr-partial`，1 个请求） | 造一个超长歌单触发升级，或 `--service-url` 指自己起的服务 |
| 酷狗自动下载安装路线（`node-service-local`） | fixture 走的是 `node-service-external`（`http://127.0.0.1:3210`），`install_method`/`resolved_digest` 都是 `null`；本次会话的 `~/.cache/playlist-dig` 不可写（见第 3 节），要跑这条路得先设 `PLAYLIST_DIG_HOME` | 设 `PLAYLIST_DIG_HOME` 指向可写目录后跑 `mpa fetch collection_…`，确认 `install_method` 与 `resolved_digest` 的值 |
| 酷狗 fixture 里 `upstream_commit = 9b6a779…` 是否等于外部服务实际版本 | 不等于。外部服务没有版本信息，该值是工具钉住的 commit 回落而来 | 只能查外部服务的部署记录；本文已在 kugou.md 里写明不能当证据 |
| 网易云「`n=1000` 是一次请求的上限」等条目级边界 | 继承观察，本次只验证了 200 首（3 个 part = 1 个 `playlist_detail_v3` + 2 批各 100 首的 `song_detail`） | 抓一个 > 1000 首的歌单 |
| QQ `songlist` 里 `album` 为 `None` 的成因、以及 `num=999` 是否真被上游尊重 | 只实测到 76 首；`num=999` 是请求参数，是否生效未验证 | 抓一个 > 999 首的歌单对比 |
| `metadata` 类水印/加密字段（如各平台新加的签名头） | 未涉及 | 上游开始校验签名时错误会以 HTTP 4xx/空成功形式出现，见各文件的「失效症状」 |

标成 D 的条目已在对应文档正文里就地标注（例如「继承自 v1 的观察，本次未复核」），本文不重复列举全部条目。

---

## 5. 把 D 级主张升级成 B 级的最小流程

```
mpa doctor --provider <provider> --live --id <确认存在的公开 id>   # 先确认接口还活着
mpa fetch <input> --out /tmp/chk
mpa verify /tmp/chk --strict                                      # 判定项全 ok 才算可复算
python3 tools/contract_diff.py tests/fixtures/<provider> /tmp/chk  # 形状仍与入库 fixture 一致（退出 0）
mpa export /tmp/chk --format csv | head                          # 取到的字段长什么样
```

最后一步 `contract_diff` 是「今天抓的这份形状还对」的证据：`verify --strict` 只证明「这份新 `raw/` 能复算出这份新 `playlist.json`」，对「平台是否还返回那些字段、总数还在不在」不发言（原因见 [schema.md](./schema.md) 第 8.3 节）。

然后把 `/tmp/chk` 关键响应（或整目录）留档成 fixture，并在文档里把「最后验证」改成当天日期、把「继承观察，未复核」的措辞删掉。**不要在没有 `mpa verify --strict` 通过的情况下把一条主张写成已验证。**

---

## 维护提示

```
python3 -m mpa verify tests/fixtures/netease
python3 -m mpa verify tests/fixtures/qq
python3 -m mpa verify tests/fixtures/kugou
python3 -m mpa verify tests/fixtures/qishui
python3 tools/rederive_fixture.py tests/fixtures/*          # 只改了 derive 解释逻辑时；应全部「未改写」
python3 tools/gen_readme_numbers.py --check                 # 覆盖表与 fixture 是否仍然一致
```

四条 `verify` 全部 `ok` ＝本文第 2 节的 B 级证据仍然成立；任何一条出现 `FAIL` ＝**对应平台的文档或被引用的 fixture 需要重读，先看该平台 reference 的「失效症状」一节**。注意 `[warn] adapter_version_ok` 不是失败：它表示数据由旧版本适配器抓取，默认判定仍通过（加 `--strict` 才会因此失败）。

上面这些命令都只跟**已入库的 `raw/`** 打交道，全都无法发现「平台今天又变了」。要发现那个，必须拿一次新抓取去比形状：`python3 tools/contract_diff.py tests/fixtures/<provider> <新抓目录>`，退出码 0 才算形状没变（判据见 [schema.md](./schema.md) 第 8.3 节）；**不要拿 `raw_digest` 代替它**，汽水页面每次请求都不同，而派生结果除曲风（`extra.genre`/`extra.subgenre`）外可以逐字节相同。
