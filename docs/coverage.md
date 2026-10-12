# 字段覆盖情况（自动生成）

这张表回答一个问题：**每个平台到底给了哪些字段、给了多少条**。数字全部由
`tests/fixtures/` 里的真实抓取记录算出（`python3 tools/gen_readme_numbers.py`），CI 会检查它
有没有过期——手写这种表，三天就会开始骗人。

「声明 / 实取」那一行是这张表最该看的地方：声明数与实取数对不上，就说明有缺口（退出码 1）。
平台专属字段（`extra`）与歌单级字段（`playlist.extra`）分开列，因为作用域不同。

<!-- coverage:begin -->
<!-- 由 tools/gen_readme_numbers.py 生成，请勿手改；CI 会检查是否过期 -->

| 字段 | `netease` | `qq` | `kugou` | `qishui` |
| --- | --- | --- | --- | --- |
| `duration_ms` 时长 | 200/200 | 76/76 | 50/50 | 65/65 |
| `language` 语种 | 0/200 | 76/76 | 50/50 | 65/65 |
| `released_at` 发行日期 | 200/200 | 76/76 | 50/50 | 65/65 |
| `collected_at` 收藏时间 | 0/200 | 0/76 | 50/50 | 0/65 |

| 声明 / 实取 | 200 / 200 | 76 / 76 | 50 / 50 | 65 / 65 |
| --- | --- | --- | --- | --- |
| 可用 / 不可用 | 200 / 0 | 76 / 0 | 50 / 0 | 65 / 0 |
| 抓取方式 | `direct-http` | `direct-http` | `node-service-external` | `ssr-partial` |

平台专属字段（`Track.extra`，分母是该平台实取到的曲目数）：

| `extra` 键 | `netease` | `qq` | `kugou` | `qishui` |
| --- | --- | --- | --- | --- |
| `album_type` | — | 76/76 | — | — |
| `alias` | 39/200 | — | — | — |
| `bpm` | — | — | 50/50 | — |
| `composers` | — | — | — | 63/65 |
| `fileid` | — | — | 50/50 | — |
| `genre` | — | — | — | 65/65 |
| `label` | — | 75/76 | — | — |
| `lyricists` | — | — | — | 63/65 |
| `popularity` | 200/200 | — | — | — |
| `remark` | — | — | 50/50 | — |
| `subgenre` | — | — | — | 56/65 |
| `trans_name` | 17/200 | — | — | — |

歌单级字段（`playlist.extra`，描述的是歌单本身，不是每首歌）：

| `playlist.extra` 键 | `netease` | `qq` | `kugou` | `qishui` |
| --- | --- | --- | --- | --- |
| `created_at` | 2013-09-09T10:10:06Z | 2019-05-28T01:45:35Z | 2026-01-29T15:14:20Z | 2024-08-02T04:02:53Z |
| `published_at` | — | — | 2026-01-29 | — |
| `tags` | — | — | 流行、网络、甜蜜、精选 | — |
| `track_updated_at` | 2026-09-29T12:32:09Z | — | — | — |
| `updated_at` | 2026-09-29T00:22:25Z | — | 2026-02-13T12:19:44Z | 2026-09-29T15:44:42Z |

上面每一列都对应仓库里的一份真实抓取记录：`tests/fixtures/<平台>/`，里面同时存了 `raw/`（原始响应）、`playlist.json`（推导结果）和 `provenance.json`（出处）。任何人都可以用 `mpa verify tests/fixtures/<平台>` 在断网环境下重新算一遍，对不上就会失败。
<!-- coverage:end -->

生成与校验：`python3 tools/gen_readme_numbers.py`（写回本文件）、
`python3 tools/gen_readme_numbers.py --check`（只检查，CI 跑的就是这条）。
