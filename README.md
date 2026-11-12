# GitHub 项目面板（本机）

监控账号下所有仓库与上游的差异，区分「纯 fork」和「有自有提交」，并提供一个更新入口。

## 启动 / 停止

- 双击 `start_panel.bat`：后台启动服务并打开浏览器
- 双击 `stop_panel.bat`：停止服务
- 地址：http://127.0.0.1:8770/

命令行方式：

```bash
python dashboard.py            # 前台运行
python dashboard.py --port 9000
```

## 规则

| 类型 | 判定 | 「更新」按钮的行为 |
|---|---|---|
| 纯 fork | `自己独有 = 0` | 直接调 GitHub 官方 `merge-upstream`（等同网页 Sync fork） |
| 有自有提交 | `自己独有 > 0` | 只跳转项目主页，由你自己合并 |
| 非 fork / 无上游 | 没有 parent | 只展示，不参与比对 |

- 「缺上游」= 上游有、你没有的提交数；> 0 时该行标红并排到最前
- 已归档（archived）的仓库不纳入监控
- 上游是否停维护：上游仓库被归档，或上游描述/README 里出现自指的停止维护声明（如「本项目已停止维护」）；仅历史叙述（「原作者停止维护后…」）不会误报
- 数据缓存 5 分钟；右上角「刷新」强制重新拉取
- 勾选「只看有更新」隐藏已同步的行

## 依赖

- 已登录的 `gh` CLI（`gh auth status`）
- Python 3（只用标准库）

## 换电脑怎么用

1. 装 Python 3（安装时勾选 **Add python.exe to PATH**）和 [GitHub CLI](https://cli.github.com/)
2. 登录：`gh auth login`（选 GitHub.com / HTTPS / 用浏览器登录）
3. 设置要监控的账号（必须，代码里不再写死）：
   - Windows：`setx FORK_WATCH_OWNER 你的用户名`，或启动时传参数 `start_panel.bat 你的用户名`
   - macOS / Linux：`FORK_WATCH_OWNER=你的用户名 python3 dashboard.py`
4. 克隆本仓库，双击 `start_panel.bat` 即可

macOS / Linux 没有 `.bat`，直接 `FORK_WATCH_OWNER=你的用户名 python3 dashboard.py`，浏览器打开 `http://127.0.0.1:8770/`。

## 手机 / 其他电脑访问（Tailscale）

`start_panel.bat` 会自动检测 Tailscale：

- 有 Tailscale：只监听 Tailscale 地址（如 `100.75.244.123`），浏览器也打开这个地址
- 没有 Tailscale：回落到 `127.0.0.1`

所以在同一 tailnet 的手机/电脑上直接打开：

```
http://<这台机器的 Tailscale IP>:8770/
```

查本机 Tailscale IP：`tailscale ip -4`。

也可以手动指定监听地址：

```bash
python dashboard.py --host 0.0.0.0      # 局域网 + Tailscale 都能访问
python dashboard.py --host 100.x.y.z    # 只监听 Tailscale
```

面板没有登录验证，能打开页面的人就能触发 fork 更新；只在自己的 tailnet/可信网络里用。