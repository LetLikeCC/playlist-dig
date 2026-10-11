# 变更记录

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
