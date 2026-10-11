# Security Notes

## 本仓库会做什么

- 网易云 / QQ音乐 / 酷我(波点) 三条路线：**直连平台公开端点**，只读歌单元数据，不在本机运行任何第三方服务。
- 酷狗路线：运行时把 [MakcRe/KuGouMusicApi](https://github.com/MakcRe/KuGouMusicApi)（MIT）
  **按固定 commit** 取到私有缓存目录 `~/.cache/playlist-dig/services/<repo>/<commit>/`，
  在 `127.0.0.1` 的**随机空闲端口**上启动，用完立即关闭。缓存目录可用 `PLAYLIST_DIG_HOME` 覆盖
  （沙箱/CI 里 `$HOME` 不可写时必须覆盖）。
- 汽水（>175 首的歌单）路线：同理运行 [guowenye/qishui-api](https://github.com/guowenye/qishui-api)（MIT）。
- 抓取到的响应会**原样落盘**在输出目录的 `raw/` 里（`.txt` 逐字节、`.json` 按解析结果紧凑重写），
  这是「可离线复算」这条设计标准的前提；里面可能含他人昵称、歌单标题等公开可见信息。

## 你需要知道的风险

1. **供应链**：装依赖会执行上游项目及其依赖的安装期脚本。本项目通过**固定 commit**
   （`ServiceSpec.commit`，与 `fetch.log.json.upstream_commit` 一致）+ 优先
   `pnpm install --frozen-lockfile` 收敛这一风险，但无法归零；上游仓库没有可用的 lockfile 时
   会退回 `npm install`，并把实际用了哪种记进 `ctx.extra.service.install_method`。
   离线/敏感环境请勿运行这两条路线。
2. **上游能力**：`qishui-api` 上游自带音频下载与解密模块。本项目**只调用其歌单详情接口**，
   不调用、也不维护那些能力；使用它们的责任在上游声明的使用边界内由使用者自担。
3. **端口**：服务只监听 `127.0.0.1` 且端口由工具挑选的空闲端口；命令结束即关闭。若进程异常残留，
   先找端口再按 pid 精确清理（不要全杀 node）；服务日志在 `<缓存目录>/logs/`：
   - Windows: `netstat -ano | findstr <port>` → `taskkill /PID <pid> /F`
   - macOS/Linux: `lsof -ti tcp:<port>` → `kill <pid>`
4. **数据出境**：抓取结果（曲目清单）会被 agent 读入上下文并发送给你所用的模型服务商。

## 如何禁用这两条路线

不安装 node/npm 即可——届时酷狗与汽水大歌单路线会在部署服务前明确报错退出，
其余三条路线不受影响。

## 报告问题

请通过 GitHub issue 报告，**不要**在 issue 中粘贴包含他人 uid/昵称的报告内容。
