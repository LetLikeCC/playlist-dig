# 测试怎么跑、测什么、不测什么

零依赖、纯标准库 `unittest`（**没有 pytest，也不要加**）。整套测试**不发任何网络请求**，
全部跑在入库的抓取记录（`fixtures/`）或伪造对象上，也不写入 `fixtures/` 之外的任何固定路径。

```bash
cd playlist-dig

python3 -m unittest discover -s tests            # 全部 476 条，约 2.0 秒
python3 -m unittest discover -s tests -v         # 看每条名字
python3 -m unittest tests.test_pipeline          # 单个文件
python3 -m unittest tests.test_pipeline.TestExitCodes.test_a_gap_is_partial     # 单条
python3 -W error::ResourceWarning -m unittest discover -s tests   # 顺手当资源泄漏检查
```

输出应该是 `Ran 491 tests in ~2.0s` + `OK`（这个条数由 `tools/gen_readme_numbers.py` 维护，
`--check` 在 CI 跑——手写会漂，实测漂过一次）。本机验证用的是 Python 3.14.7；CI 跑 3.10 与 3.13，
所以测试里不用版本相关语法（类型注解一律靠 `from __future__ import annotations`）。

为什么不用 `pytest`：这个工具的卖点是「零依赖」，测试跟着一起零依赖，克隆下来就能跑；
`tests/` 里没有 `__init__.py`，每个模块自己 `sys.path.insert` 自己的目录再 `import _support`，
所以 `discover -s tests` 从仓库根目录跑即可。

## 文件分工

| 文件 | 条数 | 覆盖的东西 |
| --- | ---: | --- |
| `test_model.py` | 52 | `mpa/playlist@1`：`Field` 的四种来源与 `known`、`Track`/`Ledger`/`Playlist` 的序列化与键序（含歌单级 `extra` 的键序与往返）、常量词表，以及四份 fixture 逐条字段的来源一致性 |
| `test_errors.py` | 14 | 错误种类词表、异常层级（`RouteUnavailable`/`UpstreamChanged` 等）、`retryable`、`exit_code_for` 的判定顺序 |
| `test_rawstore.py` | 50 | `raw/`：文件名清洗、`record()` 的落盘形状与扩展名、`digest()` 的稳定性、`_manifest.json` 的校验、错扩展名/多余文件/缺件（`DatasetError`）、`finish()` 清理 |
| `test_runtime.py` | 35 | HTTP 客户端：重试次数与退避、限速、状态码→错误种类、401/403→`blocked`、404→`notfound`、`expect="json"` 解析失败→`parse`、`finish()` 的请求日志 |
| `test_pipeline.py` | 66 | `fetch` = `collect`+`derive`、写盘内容、退出码、`declared` 缺失的处理、`load_dataset` 的报错、`verify` 对篡改的 `playlist.json`/`raw/` 的反应、`restore_fetch_meta` |
| `test_adapters_offline.py` | 68 | **最强的一档**：对每份 fixture 用磁盘上的 `raw/` 重跑 `derive`，要求与 `playlist.json` 逐字节一致；适配器元数据；`TestExtrasAreHonest` 双向核对 `capabilities` 与两处 `extra`（不许吹牛、不许漏报）并钉住每个新字段的覆盖率；汽水的 `_creator`/`_description`/`_genres`/`_credits`/`_language` 与 175 首 SSR 上限的回退分支、非 dict 标签抛 `UpstreamChanged` |
| `test_adapters_matching.py` | 46 | `find_adapter` 只认输入：各平台 URL/ID 形态、歧义数字 ID、未知输入与未知 provider 的报错 |
| `test_export.py` | 36 | `canonical_json` 的换行与键序、jsonl/csv/txt 的输出（含 CSV 的 `extra` 列与 `playlist_*` 列）、`unknown format` 报出可用格式、`limit` 只截 txt 列表且写明总数、`--out -`/空串归 stdout |
| `test_cli.py` | 47 | 各子命令的参数、错误怎么打给人看、退出码、`--json` 形状、`--out`/`--limit`、缺件的数据集一行报错、`doctor` 的 `ok/skip/FAIL` |
| `test_doc_refs.py` | 14 | 文档引用守卫（A 类提交）：`SUBCOMMANDS` 与解析器不许分叉、仓库当前必须是绿的，以及四类问题（子命令 / `.py` 路径 / 链接 / 章节号）各自都能被抓到 |
| `test_coverage.py` | 6 | 字段覆盖统计：只数已知值、平台不给就是 0（不是没有这一项）、来源分布加起来等于曲目数、extra 的声明与覆盖、**与 docs/coverage.md 逐格一致** |
| `test_contract_diff.py` | 42 | 形状比较：曲目键、规范字段的 `source` 覆盖率、`extra` 键的整批消失、`declared` 与请求计划 |
`_support.py` 里的 `EXPECTED` 是**唯一真相表**：每份 fixture 的曲目数、摘要、请求数、
mode、`capabilities`、允许出现的 extras 键都写在那里。要改期望值，只改这一张表，
不要在某个测试里另开一份白名单（这个坑踩过一次：`extra_fields | {"popularity"}`）。

## fixtures 是什么

`fixtures/<平台>/` 是**真实抓取的记录**，不是手写的样例；每份都有 `provenance.json` 说明来历，
`fixture_kind` 一律是 `recorded-live`，抓取时间都在 **2026-09-29**：

| 平台 | 输入 | 抓取时刻 (UTC) | mode | 声明/取到/可用 | 请求 | `upstream_commit` |
| --- | --- | --- | --- | --- | ---: | --- |
| netease | 热歌榜 `3778678` | 2026-09-29T17:39:35Z | `direct-http` | 200 / 200 / 200 | 3 | — |
| qq | 歌单 `7011264340` | 2026-09-29T17:41:26Z | `direct-http` | 76 / 76 / 76 | 4 | — |
| qishui | 分享页 `playlist_id=7397692920558452788` | 2026-09-29T17:46:36Z | `ssr-partial` | 65 / 65 / 65 | 1 | — |
| kugou | `collection_3_2132041455_14_0` | 2026-09-29T17:53:05Z | `node-service-external` | 50 / 50 / 50 | 2 | `9b6a779abfc9b4590ed671836c82db398fde5929` |

最后一列是 `playlist.json.source.upstream_commit`。酷狗那行的含义要小心：外部服务报不出自己的版本，这个值是适配器钉住的常量；汽水那格是「—」（SSR 路线没有这个键），但它的 `fetch.log.json` 里仍写着 `e409c10f…`。三个位置的对照见
[docs/schema.md](../docs/schema.md) 第 5.1 节末。

每个目录固定四个文件：

* `raw/` —— 原始响应 + `_manifest.json`（顺序、名字、大小、sha256）。qq 的专辑详情节有 3 份，
  每份还多一个 `*_req.json` 存**请求体**，因为 `derive` 要读它；汽水那一份是 HTML，存成 `.txt`
  （逐字节）；`.json` 那些是解析后重新紧凑序列化的结果，内容等价、不是字节等价。
* `playlist.json` —— `derive` 的输出，`verify` 的比对目标。
* `fetch.log.json` —— 当时的请求日志与 `adapter_version`。
* `provenance.json` —— `input`/`ref`/`mode`/时刻/数量/请求清单/`raw_digest`/说明。

测试只覆盖取数：夹具复算、契约、退出码、导出格式，没有任何「长什么样好看」类的用例。

`kuwo` **一份 fixture 都没有**：这条链路从未跑通，只有从 H5 bundle 反查出来的路径与请求头。
测试里只断言「它被标注为未实测」，不假装它有记录。

配套的离线工具（都在 `tools/`，只读）：

```bash
python3 -m mpa verify tests/fixtures/qq                 # 断网复算，对不上就失败
python3 tools/make_provenance.py tests/fixtures/qq --check   # 出处是否还自洽
python3 tools/rederive_fixture.py tests/fixtures/qq     # 只改了 derive 时重算 fixture
python3 tools/contract_diff.py tests/fixtures/qq /tmp/刚抓的  # 上游形状变了没有
```

### 加一份新 fixture

1. 真抓一次（`mpa fetch`，必要时 `mpa doctor -p <平台> --live --id <id>` 先探路）。
2. `python3 tools/make_provenance.py <输出目录>` 生成 `provenance.json`。
3. 拷进 `tests/fixtures/<平台>/`，确认 `python3 -m mpa verify tests/fixtures/<平台>` 通过。
4. 在 `_support.py` 的 `EXPECTED`（以及需要时 `UNAVAILABLE_FIELDS`）里登记这份记录的数字。
5. 跑整套测试；如果有测试挂在旧平台事实上（比如「某平台没有某字段」），
   改成**通用断言 + 单独举例**，不要把断言放宽到「只要有值就行」。

## 故意不覆盖的行为

写清楚测不了什么，比假装覆盖了强：

* **真实网络**。`mpa doctor --live`、canary 定时任务、真实重试时序都不在套件里；
  `urlopen` 一律被伪造，`time.sleep` 一律被 `mock.patch` 掉（所以整套测试里没有 sleep）。
* **kugou 的 node 服务**。fixture 记录的是「本地 KuGouMusicApi 已经在跑」时的响应；
  服务的安装/拉起/端口占用/版本不符都没测，只测了客户端侧的契约与错误映射。
* **汽水超过 175 首的真实回退**。`RouteUnavailable` 与 `partial_ok` 的分支用伪造客户端测了，
  但「真的起一个 `qishui-api` 服务并拿到完整 175+ 首」没有跑过。
* **上游漂移**。平台改版只能靠 `contract_diff.py` 在新抓取上发现；测试只能锁住当前记录的形状。
* **kuwo 全链路**（见上，无 fixture）。
* **Python 版本矩阵**：本机只有 3.14；3.10/3.13 由 CI 覆盖，本地没法验证。
* **时区/locale/文件权限/并发**：除了 `doctor` 那条「缓存目录不可写」的用例，
  其余都假定 UTC 与普通 POSIX 权限；也没有并发跑同一目录的场景。
* **性能与体积**：没有超过 fixture 规模（最大 1.3 MB）的压力用例。

## fixture 的隐私红线（加新 fixture 前必读）

`fixtures/` 的定义不只是「真实抓取的记录」，还有一条同样重要的隐含条件：**任何人拿到它都不会看到
不该公开的东西，而且不用联网就能复算**。所以：

* **只放可以公开的歌单**：官方榜、品牌账号的歌单、你自己的公开歌单都行；
  **别人私发给你的、你自己设为私密的、带个人身份信息的，一律不放**——这类数据要么让所有者
  自己决定，要么改用不记录 id 的方式写进文档（见下条）。
* **提交前先把「私密标识清单」grep 一遍**：歌单名、uid、`playlistId`、分享短链 token、
  本机绝对路径。真实值写进测试是很自然的动作，也是本项目踩过的坑：一条单元测试里粘了真实
  分享页的歌单名和 uid，被推到公开仓库，最后只能改写提交历史。
  **写测试时用合成值（`示例歌单` / `示例用户` / `123456789`），但保留形状**（字段名、层级、
  取值类型）——否则测试就没意义了。
* **实测记录只写统计事实**：例如「用户提供的波点歌单（id 不记录）：声明 236 / 实取 202，
  gap 34」，而不是歌单名或 id。Adapter 的 `verified_note` 与 `docs/references/*.md` 都照这个来。
* **私密数据的抓取结果只留在临时目录**（如 `/tmp/...`），用完删掉，不进版本库。

## 约定

* 要破坏一份 fixture，先 `S.copied_fixture(<平台>)`（`tempfile` + `shutil.copytree`，退出时删），
  **永远不要原地改** `tests/fixtures/`。
* 需要「现在几点」时不要读表：`_NoClockDatetime` + `mock.patch` 把 `time.time/monotonic/time_ns`、
  `datetime.datetime`、`mpa.adapters.base.now_iso` 一起钉死（注意 `mock.patch("time.strftime")`
  会连带打断 `datetime.strftime`，别这么写）。
* 发现真 bug 时，产品代码可以改（`mpa/**` 与测试在同一个提交里一起改），但**改法要留证据**：
  修完的同一条测试里写清「原来是什么样、现在断言什么、为什么」，例如
  `test_rawstore.py` 里同名不同扩展名的残留文件（现在由整目录换入解决）、
  `test_pipeline.py` 里「抓取失败不许动旧数据集」、`test_adapters_offline.py` 里 `_genres`
  遇到非 dict 标签抛 `UpstreamChanged`、汽水 `lang_codes` 被写死成 `unavailable` 那一处
  「平台给了却扔掉」的修复。如果某条现状是**刻意的取舍**而不是缺陷（例如 `mpa/cli.py` 里 `[skip]`
  分支不可达、`doctor` 用 `[  ok]` 打印跳过行），docstring 要写成「行为记录」而不是「BUG」。
* 断言要表达契约，不要为了通过而放宽：与其 `assertTrue(x)`，不如写清楚数量、集合或字符串。
* 断言顺序无关的结果（集合、计数），因为字符串集合的遍历顺序随进程哈希种子变化。
  需要比顺序时（曲目顺序、`raw/_manifest.json` 的条目顺序）先确认那条顺序本身是契约。
