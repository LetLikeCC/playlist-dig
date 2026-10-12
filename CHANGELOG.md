# 变更记录

## 2.1.0

数据契约没变（`mpa/playlist@1` 与各适配器的 `2.0.0` 都不动，四份 fixture 的
`mpa verify --strict` 依旧全绿）；这一版修的是工具自己：文档会说谎、失败会污染旧数据、
退出码会被管道改写。

**抓取不再污染旧数据集**

- 响应先写 `<outdir>/raw.incoming/`，全部成功之后整目录换入 `raw/`；两个 JSON 用
  「临时文件 + `os.replace`」写。抓取中途失败时旧数据集一个字节都不动。
- 提交顺序（换 `raw/` → `playlist.json` → `fetch.log.json`）保证任何一步被打断都会让
  `mpa verify` **响亮失败**，而不是留下一个混了两个版本的目录。
- 上一次崩溃留下的 `raw.incoming/` 会在下次抓取开工时清掉并提示；`fetch.log.json` 的
  `raw_cleanup` 字段随之废弃（老数据集里可能还有，读取不受影响）。

**退出码不再被管道改写**

- `mpa fetch ... 2>&1 | head -1` 以前会因为 stderr 断管而中断抓取、却返回 0；现在进度输出
  容忍断管，抓取照常完成。
- `mpa verify <坏数据> | head -1` 以前会把「验证失败」报成 0；现在退出码只由检查结果决定。

**版本号下沉**

- 适配器版本从全局常量改为各适配器自己声明（`Adapter.version` 只作默认值）：改酷狗的解析
  不该让 netease 的 fixture 一起红。包的发布版本与适配器版本的关系、升版本的姿势写在
  [docs/schema.md](docs/schema.md) 第 5.2 节。

**canary 第一次真跑**

- `canary.yml` 在真 runner 上跑通：四个平台全绿（`drift=0 fail=0`），含 kugou 的自动 provision。
- 产物从 73 MB 降到 2.7 MB（不再把 kugou 的 `node_modules` 打进 artifact，只收日志与原始响应）。
- 新增 `verify_alerting` 手动开关，用来演练「失败时开 issue」——报警路径只有真的响过一次才算验证过。
- 抓取那一步加 `timeout-minutes: 15`，避免最重的 kugou provision 卡到 job 级超时。

**文档与守卫**

- 新增 `tools/check_doc_refs.py`：CI 每次检查文档里的子命令、`.py` 路径、markdown 链接、
  章节号是否都真实存在（同一个文档里章节号还不许重复）。这条守卫的由来是 README 长期引用
  已删命令与已删章节。
- README 从 209 行减到 142 行：覆盖表移到 [docs/coverage.md](docs/coverage.md)，
  「六条验收标准」的完整表、`collect/derive` 与 `verify` 的深入说明、canary 的形状比对理由、
  不变量抓到的真 bug 移到新的 [docs/design.md](docs/design.md)。
- 清理：删掉 `Ledger.finalize()`（早已是无操作）、适配器基类不再摸 `store._parts` 私有属性（改用公开的 `parts`）、
  `Ledger.to_obj()` 去掉三明治写法（键序不变，`verify` 是逐字节比对）。

## 2.0.0

把音乐平台的歌单取成一份可离线复算、每个字段都有出处的数据集。（版本号与代码里的
`ADAPTER_VERSION` / `mpa --version` 一致；适配器版本进 `playlist.json`，`verify --strict` 会核对它。）

**取数**

- 五个平台适配器：网易云、QQ、酷狗、汽水已实测，酷我接口已按官方 H5 bundle 反查实现（未实测）。
- `fetch = collect + derive`：适配器只发请求并把每个响应记进 `raw/`（文本按收到的字节落盘，
  JSON 按解析结果紧凑重写），推导是纯函数、只读这些记录。
- 契约 `mpa/playlist@1`：每个字段带 `source`（`platform`/`derived`/`unavailable`）与原因；
  拿不到的字段写 `unavailable` + `reason`，不填占位值、不猜。
- 数量账本 `ledger`：`declared`/`fetched`/`ok`/`unavailable`/`gap`，差额与不可用曲目如实记；
  平台不给总数时 `declared` 保持 `None`，绝不用「取到的条数」顶替。
- 酷狗与汽水的本地服务（node）自动 provision 并固定 commit；汽水分享页 175 首的硬上限会被
  「声明数 vs 实取数」发现并报退出码 1，而不是把 175 当成全部。

**校验**

- `mpa verify [--strict]`：断网重跑 `derive` 并与 `playlist.json` 逐字节比对，五项检查
  （schema、`raw/` 摘要与清单、`playlist.json` 记录的摘要、复算一致、适配器版本）。
- 数据集缺件或损坏给一行中文错误 + 退出码 2，不是 traceback；`derive` 发现上游形态变了抛
  `UpstreamChanged` → 退出码 3。
- `mpa doctor [--live]`：环境自检 + 真发请求验证「平台声明可用但一条都没取到」的字段。
- `tools/contract_diff.py`：只比形状（曲目键、字段来源覆盖率、`extra` 键、`declared`、请求计划），
  供定时 canary 发现上游漂移。

**导出**

- `mpa export --format json|jsonl|csv|txt`：`csv`/`jsonl` 每行带 `*_source` 列，
  曲目级 `extra` 压在 `extra` 一列、歌单级字段另占 `playlist_*` 列；`--limit` 只截 txt 列表。

**仓库内自带的东西**

- 四份真实抓取记录（`tests/fixtures/`，含 `raw/`、`playlist.json`、`provenance.json`）：
  任何人 clone 下来不联网就能复算。
- 462 条离线测试（模型、错误码、`raw/` 存储、重试、各平台 `derive`、导出、CLI、形状比较）。
- GitHub Actions：`ci.yml` 每次提交跑测试 + 四份 fixture 复算 + 出处复核 + README 数字；
  `canary.yml` 定时真发请求，发现上游变了就开 issue。
