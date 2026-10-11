# NOTICE — 第三方项目署名与许可 (Third-Party Notices)

本项目在发布和使用时声明以下外部依赖与数据来源。引用他项目的成果时保持署名，
是本仓库能成立的前提，请下游使用者继续保留本文件。

## 1. MakcRe/KuGouMusicApi（酷狗路线的核心依赖）

- 仓库：https://github.com/MakcRe/KuGouMusicApi
- 用途：酷狗音乐 Node.js API 逆向服务。`mpa/adapters/kugou.py` 里的 `SERVICE`/`ServiceSpec` 会按**固定 commit**
  （`9b6a779abfc9b4590ed671836c82db398fde5929`）下载其源码包并在本地
  启动该服务，用它提供 `/playlist/detail` 与 `/playlist/track/all` 等接口来获取歌单全量曲目。
- 许可：**MIT License**（已验证，取自其仓库 LICENSE 文件）
- 署名方式：本文件、SKILL.md、README.md 及脚本 docstring 均注明来源与链接；
  下游分发请保留以下声明：

> Kugou playlist fetching in this project relies on
> [MakcRe/KuGouMusicApi](https://github.com/MakcRe/KuGouMusicApi) by MakcRe,
> licensed under the MIT License. All credit for the reverse-engineered Kugou
> API implementation belongs to the upstream project; this repository only
> automates its deployment and playlist-id decoding.

- 上游许可证全文见其仓库：https://github.com/MakcRe/KuGouMusicApi/blob/main/LICENSE

## 2. guowenye/qishui-api（汽水音乐路线的核心依赖）

- 仓库：https://github.com/guowenye/qishui-api
- 用途：汽水音乐 Node.js API 服务。`mpa/adapters/qishui.py` 在歌单超过分享页
  SSR 内嵌上限（175 首）时，自动获取该项目的**固定 commit**
  （`e409c10fe7441a10d370da7da61d62bbc1e5126c`）到私有缓存目录并在本地启动，用其 `/playlist/detail` 接口获取歌单全量曲目。
- 许可：**MIT License**（已验证，取自其仓库 LICENSE 文件，版权人 `Copyright (c) 2026 arrow`；
  若将来 vendor 上游任何代码，必须保留该版权声明）
- **能力披露**：上游服务自带音频下载与解密模块（download/decrypt 等）；
  本项目**只调用其歌单详情接口**，不调用、不维护那些能力，使用责任在上游声明的
  使用边界内由使用者自担。
- 署名方式：本文件、SKILL.md、README.md 及脚本 docstring 均注明来源与链接；
  下游分发请保留以下声明：

> Qishui (Soda Music) playlist fetching in this project relies on
> [guowenye/qishui-api](https://github.com/guowenye/qishui-api),
> licensed under the MIT License. All credit for the reverse-engineered
> Qishui API implementation belongs to the upstream project; this repository
> only automates its deployment.

- 上游许可证全文见其仓库：https://github.com/guowenye/qishui-api/blob/main/LICENSE

### 钉住的 commit 记在哪里

两个服务的钉住版本都写在各自适配器的 `ServiceSpec.commit` 里，并随数据一起留痕：
`fetch.log.json.upstream_commit` 记的是**代码钉住的目标**，`playlist.json.source.upstream_commit`
记的是**这次运行实际用的版本**（只有工具自己下载服务时才知道；用 `--service-url` 指向别人起的
服务时是 `null`）。两处为什么不同、四个 fixture 分别是什么值，见 `docs/schema.md` 第 5.1 节末。

## 3. 数据来源与所属权利

- 网易云音乐、酷狗音乐、QQ音乐、汽水音乐、酷我音乐 的歌单数据归属各平台及相应权利人。
- 本项目调用的接口均为**非官方逆向的公开端点**，未获得任何平台官方授权；
  本项目与 网易云音乐（网易）、酷狗音乐 / QQ音乐 / 酷我音乐（腾讯音乐）、
  汽水音乐（抖音/字节跳动）无任何隶属关系。
- 各平台名称与商标仅用于描述兼容对象，不构成权利主张。

## 4. 使用边界

- 仅限个人学习、研究与娱乐性分析（如"分析我的听歌人格"）。
- 只处理用户主动分享的、公开可见的歌单；不得用于抓取他人隐私、批量爬取、
  商业化转售数据或任何违反平台服务条款的场景。
- 逆向接口随时可能失效或变动；使用者需自行承担调用产生的合规风险。
