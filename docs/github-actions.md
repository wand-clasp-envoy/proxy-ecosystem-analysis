# 在 GitHub 上自动跑（Actions 部署）

目标：把 `harvest → organize → publish` 变成定时任务，让订阅地址永远是最新的。
仓库里已经准备好两样东西：

```
.github/workflows/harvest.yml   # 定时（每 6h）+ 手动触发
tools/publish.py                # 把产出的 organised.yaml 推到 gist（=订阅地址）
requirements.txt                # 只有 PyYAML
```

## 1. 一次性配置

### 1.1 加 secrets

仓库 → **Settings → Secrets and variables → Actions → New repository secret**：

| Secret | 必填 | 用途 |
|---|---|---|
| `GIST_TOKEN` | 发布时必须 | 生成/更新 gist。Classic PAT 勾 **`gist`** 即可；fine-grained 选 **Gists: Read and write** |
| `GIST_ID` | 首次可空 | 目标 gist 的 id（`https://gist.github.com/<user>/<id>` 里那串）。**留空则首次运行自动创建一个**，日志里会打印新 id |
| `GH_PAT` | 可选 | 只读 gist 用 `GITHUB_TOKEN` 就够（1000 次/小时/仓库）；如果你的源多到超限，加一个纯 `gist` 的 PAT 提到 5000 次/小时 |

> ⚠️ `GITHUB_TOKEN` **不能创建 gist**（它只对仓库有权限），所以发布这一步必须用 `GIST_TOKEN` 这个 PAT。

### 1.2 触发一次

Actions → **harvest** → **Run workflow**：

| 输入 | 默认 | 说明 |
|---|---|---|
| `pages` | `3` | 每个查询词翻几页（每页 10 条），10 个词 × 3 页 ≈ 215 个候选 |
| `health` | `30` | 丢掉名字里自报健康度 < 30% 的节点 |
| `speed` | `1` | 丢掉自报速度 < 1 MB/s 的节点 |
| `mode` | `refresh` | `full` 会额外跑 `daily.py`（24h 存活）+ `churn.py` + `formats.py`，多约 300 次 API 调用 |

跑完后：

- **Summary 页**会显示这次的门限漏斗（`13,988 → 7,816` 这种）
- `docs/feeds.md` 会被 bot 自动提交回 `main`（只有这一个文件，`data/` 在 `.gitignore` 里）
- 如果配了 `GIST_TOKEN`，日志末尾会给出：

```
raw:  https://gist.githubusercontent.com/<user>/<gist_id>/raw/organised.yaml
```

这就是订阅地址，直接填进 Clash / mihomo / Stash：

```yaml
# 客户端里粘贴
https://gist.githubusercontent.com/<user>/<gist_id>/raw/organised.yaml
```

## 2. 它到底怎么跑

```
schedule 17 */6 * * *  ──┐
workflow_dispatch ───────┴─→ checkout → python3.12 → pip install
                              → harvest.py search   (gist.github.com，直连即可)
                              → harvest.py meta     (api.github.com)
                              → fetch_nodes.py      (46 个源 + 传输指纹)
                              → organize.py         (归一/去重/重命名/分组)
                              → report.py           (docs/feeds.md + summary.json)
                              → publish.py          (gist = 订阅)
                              → git commit docs/    (只在有变化时)
```

## 3. 必须知道的 6 个限制

1. **cron 最小 5 分钟，且经常延迟 5–20 分钟**。当前设的是每 6 小时（`:17`），够用；别写 `*/5`，既不准也浪费。
2. **60 天没有仓库活动，schedule 会被自动停用**。这个 workflow 每轮都会尝试提交 `docs/feeds.md`，本身算活动；即使 docs 没变，手动跑一次也能重置计时。
3. **公共仓库免费**（分钟数不限）；私有仓库 2000 分钟/月，一轮约 3–6 分钟，每 6 小时 ≈ 360–720 分钟/月，够但不宽裕。
4. **发布出去的节点凭据是公开的**（secret gist 只是不进搜索，知道 URL 的人都能读）。不要往里塞私人机场订阅；要私密就改成「产物上传 artifact + 私有仓库」。
5. **分支保护会挡住 bot 的 `git push`**。开了保护就在 Settings → Actions → General 里允许 `github-actions[bot]` 推送，或者把提交步骤删掉。
6. **Actions 不是给你做高频抓取的**。每 6 小时一次是正常 CI 节奏；每分钟一轮属于滥用，会被限流甚至封号。

## 4. 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| `skipping publish: GIST_TOKEN secret is not set` | 没配 secret | 加 `GIST_TOKEN` |
| `HTTP 403: token missing gist scope` | PAT 没勾 `gist` | 重新生成 |
| `HTTP 404: gist not found` | `GIST_ID` 错 / token 不是该 gist 的属主 | 核对 id，或留空让脚本新建 |
| `HTTP 403 ... rate limit` | 未认证 60/h 或 token 额度用完 | 配 `GH_PAT` |
| `search ... ERR <urlopen error>` | runner 访问 `gist.github.com` 失败 | 一般不会；如偶发，重跑即可 |
| 节点数突然掉到 0 | 上游 gist 全被删/改名 | 看 `data/candidates.json` 的命中数 |

## 5. 备选部署方式

| 方式 | 适合 | 说明 |
|---|---|---|
| **GitHub Actions（本方案）** | 没有服务器、想零成本 | 抓取 + 发布都跑在 runner 上，产物进 gist |
| VPS + cron / systemd timer | 要高频（10 分钟级）、要主动探测 | 直接跑 `tools/` 里的脚本，写本地文件，用 nginx/caddy 提供 `/sub` |
| Cloudflare Worker Cron | 只想定时触发 | Worker 不能跑 Python；适合做 gist 反代（subs-check 的 worker 就是这个用途） |
| 上游 subs-check 容器 | 想要整套现成能力 | `beck-8/subs-check` / `sinspired/subs-check-pro`，自带 WebUI、sub-store、R2/Gist 上传 |

## 6. 本地等价命令

Workflow 里每一步都能在本地跑（本地访问 `gist.github.com` 需要代理）：

```bash
export GIST_PROXY=http://127.0.0.1:7897      # 仅本地；CI 里不要设
export GH_TOKEN=$(gh auth token)
cd tools
python harvest.py search --pages 3 && python harvest.py meta && python fetch_nodes.py
python organize.py --cache --health 30 --speed 1 && python report.py
python publish.py --gist <id> --file data/organised.yaml     # 或省略 --gist 新建
```
