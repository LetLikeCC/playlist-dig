# playlist-dig（命令行工具 `mpa`）

把一个音乐平台的歌单，取成**一份能被人反复核算的数据**：每个字段都说得出是从哪个响应里来的，数量对不上会明说，原始响应全部留档（文本原样落盘，JSON 按解析结果紧凑重写，内容等价），断网也能重新推导一遍。

它产出的是一个**数据集目录**（`playlist.json` + `fetch.log.json` + `raw/`）和几种数据格式的导出；取数链路里不做任何猜测——拿不到就写 `unavailable` 并说明原因，绝不填一个看起来合理的值。想拿它做统计、报告还是图表，是下游自己的事。

更早那版（各平台的抓取脚本 + 一个检查脚本）问题意识是对的——把歌单取全、说清哪些取不到——但实现上有几处硬伤：文档与脚本混在一起、正文里夹着大段 prompt 文本、零测试、五个平台共用一堆复制粘贴的 `requests` 调用、`实测` 数字无法复核、README 的安装步骤照做会失败。这一版保留问题意识，把「可复核」做成结构：`fetch = collect + derive` 的拆分、四份真实抓取记录入库、491 条测试、版本化契约 `mpa/playlist@1`。

## 30 秒上手

```bash
cd playlist-dig
python3 -m mpa fetch "https://y.qq.com/n/ryqq/playlist/7011264340" --out ./out   # 网易云/QQ/酷我给链接即可
python3 -m mpa verify ./out              # 断网复算：raw/ 能不能推出同一份 playlist.json
python3 -m mpa export ./out --format csv --out tracks.csv   # 导出 json/jsonl/csv/txt
```

`fetch` 的输出目录就是交付物本身，可以直接丢进 git、发给别人、或当回归输入：

```
out/
  playlist.json     # 唯一的事实来源：mpa/playlist@1
  fetch.log.json    # 这次抓取的元信息（模式、上游 commit、请求数、警告、退出码）
  raw/              # 每个请求的原始响应 + _manifest.json（每项 sha256 + 总摘要）
```

## 六条验收标准

写这个工具时先定死了六条「算不算取到」，代码是为它们服务的，不是反过来：

1. **数量对不上必须说清楚**——`ledger` 里写明声明数、实取数、可用数、差额与原因，差额非 0 就退出码 1；
2. **每个字段都有出处**——`Field.source` 只有 `platform`/`derived`/`unavailable` 三种，导出里每行都带 `*_source` 列；
3. **原始响应留存、离线可复算**——`raw/` + `_manifest.json` 摘要，`mpa verify` 重跑 `derive` 逐字节比对；
4. **数据格式版本化**——`"schema": "mpa/playlist@1"`，字段变更必须升版本；
5. **上游变了能发现**——契约测试 + `mpa doctor --live` + canary，结构漂移给退出码 3，而不是「成功取到 0 首」；
6. **不猜**——取不到就是 `{"value": null, "source": "unavailable", "reason": "..."}`，没有把 null 说成 0 的字段。

核心机制是 **`fetch = collect + derive`**：`collect` 只发请求并把每个响应记进 `raw/`，`derive`
是纯函数、只读这些记录算出 `Playlist`。`mpa verify` 就是把 `raw/` 读回来再跑一次 `derive`、
与 `playlist.json` 逐字节比对。为什么这样拆、五项检查各管什么、这套不变量抓到过哪些真 bug，
写在 **[docs/design.md](docs/design.md)**。

## 作为 Agent Skill 使用

这个仓库**同时是一个 Agent Skill**：根目录的 [`SKILL.md`](SKILL.md) 就是技能文件，用的是标准
front-matter（`name` + `description`，description 里写明**什么时候该触发它**）。

- **技能范围**：抓取一条歌单、断网复算、导出数据格式、排查某个平台的接口与字段缺口。
  产出是数据集目录（`playlist.json` + `fetch.log.json` + `raw/`）与数据导出。
- **怎么接上去**（三种都行）：
  1. **让 agent 直接读仓库里的 `SKILL.md`** —— 零安装，工具只用 Python 标准库；
  2. **装进 skill 目录**，它就会在「用户要抓/备份/导出歌单」这类请求上被自动触发。
     注意 skill 发现只认**被扫描根目录的顶层**，形态是 `<root>/<名字>/SKILL.md`（或平铺的
     `<名字>.md`），**嵌套的 `**/SKILL.md` 不会被发现**——所以仓库根目录这一份不会自动生效，
     要放（或软链）到扫描根下。以 DeepSeek Harness 为例，扫描根按优先级是：

     | 优先级 | 根目录 |
     | --- | --- |
     | 100 | `<项目根>/.dsh/skills`（项目根 = 最近的含 `.git` 的祖先目录） |
     | 200 | `<项目根>/.agents/skills` |
     | 400 | `~/.dsh/skills` |
     | 500 | `~/.agents/skills` |

     所以一句话安装（软链，改动本仓库后技能自动跟着更新）：

     ```bash
     mkdir -p ~/.dsh/skills && ln -s "$PWD" ~/.dsh/skills/playlist-dig
     ```

     其它 agent 同理：放进它自己的 skill 目录，`<名字>/SKILL.md`，front-matter 至少含
     `name` 与 `description`（本仓库这份就是标准的 `name` + `description`）。
  3. **只要命令行**：`pip install -e .` 之后直接敲 `mpa`（`SKILL.md` 里两种写法都给了命令）。
- **`SKILL.md` 里写的是 agent 才需要的东西**：什么时候触发、哪些事不能做（不猜语种、不把社交
  计数当热度、不拿 `raw_digest` 跨时间比）、退出码怎么读、往哪儿加新平台、加之前要守哪条不变量。
- 技能负责把数据**拿全、拿得可复核**；统计/报告/画像这类分析，读 `playlist.json` 或 csv 导出
  在下游做（技能里也是这么写的，免得 agent 在这层做推断）。

## 字段覆盖情况（自动生成）

每个平台给了哪些字段、给了多少条、声明数与实取数差多少，见 **[docs/coverage.md](docs/coverage.md)**
——那张表由 `tests/fixtures/` 的真实抓取记录生成，CI 会拦「改了适配器忘改表」。

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
| `mpa export DIR --format json\|jsonl\|csv\|txt` | 导出数据格式，默认 stdout；`--out FILE` 写文件；`--limit` 只截 txt 列表 |
| `mpa doctor [--live] [--provider X] [--id ID]` | 自检环境与连通性；`--live` 真发请求验证字段 |
| `mpa adapters` | 列出适配器能力与最后实测时间 |

退出码是契约的一部分，方便直接塞进 CI：

| 码 | 含义 |
| --- | --- |
| 0 | 全部取到，数量对得上 |
| 1 | 取到了，但有缺口（差额、不可用曲目、平台没给总数） |
| 2 | 抓取失败 / `verify` 检查没过 / `doctor` 有 FAIL |
| 3 | 上游结构变了，需要改适配器 |
| 4 | 输入或参数不对（歧义 id、没有适配器认识、目录不是数据集、argparse 用法错误） |
| 5 | 这条路线当前不可用（分享页失效、本地服务起不来） |

库代码里没有 `sys.exit`，只有 `mpa/cli.py` 把异常映射成退出码，所以 `mpa` 也能当库用
（`fetch_playlist` / `verify_playlist` 只抛异常、只返回结果）。每个码的完整触发条件见
[docs/schema.md](docs/schema.md) 第 9 节。

## 目录结构

```
mpa/
  model.py          # mpa/playlist@1 的数据结构（Field/Track/Ledger/Playlist）
  pipeline.py       # fetch = collect + derive；verify；整目录换入的写盘
  runtime.py        # HTTP 客户端：重试、限速、错误映射、记录原始响应
  rawstore.py       # raw/ 的读写、摘要与换入
  export.py         # json / jsonl / csv / txt
  coverage.py       # 字段覆盖统计（doctor 与覆盖表共用一套口径）
  adapters/         # 每个平台一个文件；base.py 定义 collect/derive 契约
  doctor.py / cli.py
tests/              # 离线测试 + fixtures/（四份真实抓取记录，含 raw/ 与出处）
tools/              # 覆盖表生成、文档引用守卫、fixture 重算、形状比较
docs/               # schema（契约）/ design（为什么）/ coverage（覆盖表）/ references（各平台）
.github/workflows/  # ci.yml（离线全跑）/ canary.yml（定时真抓，漂移就开 issue）
SKILL.md            # 技能文件：给 agent 的「何时触发 + 规则 + 坑 + 怎么扩展」
```

只有 `playlist.json` + `fetch.log.json` + `raw/` 是交付物，可以直接丢进 git、发人、当回归输入。

## 开发与测试

```bash
python3 -m unittest discover -s tests      # 全部测试，离线、不联网、约 2 秒
python3 -m mpa verify tests/fixtures/qq    # 单看一份 fixture 能不能复算
python3 -m mpa doctor                      # 环境自检（--live 才真发请求）
```

改了适配器或文档之后还有三件事：`tools/gen_readme_numbers.py` 刷新覆盖表、
`tools/check_doc_refs.py` 检查文档引用、`tools/rederive_fixture.py` 重算 fixture。
测试分几类、怎么加一份新 fixture、fixture 的隐私红线，见 [tests/README.md](tests/README.md)。

## 已知缺口

写清楚哪里还不行，比多写一个功能重要。

- **kuwo 没有可公开的样例 id，所以 `--live` 一路跳过。** 端到端跑通过一次（用户提供的私密歌单，
  id 不记录：声明 236 / 实取 202、gap 34 如实报出、`verify --strict` 全过），但没有 `tests/fixtures/kuwo/`，
  也没有能公开复算的记录。波点分享链接形如
  `https://h5app.kuwo.cn/m/bodian/collection.html?playlistId=<id>`（酷我 App 里波点歌单 → 分享 → 复制链接），
  工具要的是其中 `playlistId` 的数字；喂普通酷我歌单页的 id 不一定能用（两套接口的 id 命名空间可能不同，
  表现是「返回空 data」）。怎么找一个公开 id、验证步骤，见 [references/kuwo.md](docs/references/kuwo.md)。
- **汽水分享页硬上限 175 首。** SSR 数据只内嵌前 175 条，翻页参数被忽略，超过就必须起本地
  `qishui-api` 服务；工具用「声明数 vs 实取数」发现它并把退出码设成 1，而不是把 175 当全部。
- **`canary.yml` 已在真 runner 上验证过**（2026-10-12：四个平台全绿、`drift=0 fail=0`，含 kugou 自动
  provision；报警路径用一次故意的失败演练过，issue 正常创建）。三次真跑的经验见 [docs/design.md](docs/design.md) 第 6 节）。它仍只在定时任务与手动 dispatch 时跑，
  所以「上游哪天变了」最快也要等下一次定时任务才知道。
- **裸数字 id 是歧义的**：网易云、QQ、酷我、汽水都用纯数字 id，没有 `--provider` 时工具拒绝猜，
  退出码 4。

下面这些是「用之前该知道」的细节，各自的完整说明在平台参考文档里：

- 网易云没有语种字段，`popularity` 恒为 `100.0`（无区分度），发行日期有回退链 → [netease.md](docs/references/netease.md)
- QQ 的语种是**专辑级**的，同专辑共享一个值，不能当 76 个独立样本 → [qqmusic.md](docs/references/qqmusic.md)
- 酷狗的 `gcid_*` 分享链接要走**有验证的探测**（候选 idx 1..5 + 身份校验）→ [kugou.md](docs/references/kugou.md)
- 汽水分享页每次请求都不同（`raw_digest` 只是当次快照），`extra.genre` 会摇 → [qishui.md](docs/references/qishui.md)

## 许可与致谢

上游接口知识来自各平台自己的 H5 页面与社区服务（`KuGouMusicApi`、`qishui-api`），这些项目的许可与风险由其自身承担；本工具只做只读抓取，不下载音频、不做解密。
