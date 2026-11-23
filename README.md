# proxy-ecosystem-analysis

分析开放代理生态里「节点是怎么被发布、命名、去重、筛选、分组」的，并给出一套可复现的整理流水线。

语料：GitHub Gist 上按 `updated` 排序的 `clash` / `mihomo` / `subs-check` 等搜索快照
（2026-11-23）：**214 个 gist → 46 个节点源 → 14,026 条节点记录**。

## 主要发现

1. 生态标准产物是三件套：`all.yaml`（节点）+ `base64.txt`（跨客户端，**有损**）+ `mihomo.yaml`（带分组规则）。
2. 节点名是事实标准 ABI（`🇭🇰HK_1|8.4MB/s|YT-HK|GPT⁺-TW`），但**同一节点 55% 概率在两轮更新间被改名**——名字不能当 ID。
3. 身份要用「传输指纹」（除 `name` 外全字段）。单源内重复 3.4%，跨源仅 5.8%：跨源是长尾，不是重复。
4. 选源边际收益陡峭：**3 个源覆盖 50%，5 个覆盖 80%，11 个覆盖 95%**。
5. 池子换血极快：24h 存活中位数 **20%**，需要滚动复测，不能收录一次长期使用。
6. 发布者不筛质量：带健康度的节点里 **47% 是 0%**。

完整分析见 **[docs/node-organization.md](docs/node-organization.md)**，源清单见 [docs/feeds.md](docs/feeds.md)。

## 目录

```
docs/node-organization.md   分析报告（结论 / 命名 ABI / 去重 / 质量门限 / 流水线 / 反模式）
docs/feeds.md               46 个源的节点数、存活率、更新间隔、分组模板
tools/harvest.py            搜索采集 + gist 元数据
tools/fetch_nodes.py        节点文件抓取 + 传输指纹
tools/daily.py              24h 存活、更新节奏、base64 覆盖
tools/churn.py              版本间增删/改名
tools/formats.py            base64 逐协议损失、分组模板统计
tools/organize.py           归一 + 去重 + 筛选 + 重命名 + 生成订阅
tools/report.py             汇总为 docs/feeds.md + data/summary.json
tools/publish.py            把订阅推送到 gist（Actions 里的发布步骤）
tools/summary_md.py         Actions 运行摘要
.github/workflows/harvest.yml  每 6 小时自动跑一遍
docs/github-actions.md      部署说明（secrets / 限制 / 排障）
data/                       中间产物（JSON）+ 归一后的订阅样例（仅本地，含凭据，已 gitignore）
```

## 快速开始

```bash
cd tools
python harvest.py search --pages 3                        # 网页搜索（需本地代理）
GH_TOKEN=$(gh auth token) python harvest.py meta           # API（直连更快）
GH_TOKEN=$(gh auth token) python fetch_nodes.py
GH_TOKEN=$(gh auth token) python daily.py && python formats.py && python churn.py
python organize.py --cache --health 30 --speed 1           # -> data/organised.yaml
python report.py
```

`organize.py` 支持 `--min-region`（小地区并入 🌍 其他）与 `--max-regions`（地区组上限）。

## 自动执行

已内置 GitHub Actions：每 6 小时抓取一轮，筛选后生成订阅并推送到 gist。
配置 `GIST_TOKEN`（+ 可选 `GIST_ID`、`GH_PAT`）即可，步骤见
**[docs/github-actions.md](docs/github-actions.md)**。

## 说明

本仓库是生态结构分析，不做节点可用性保证；`data/organised.yaml` 是抓取快照的归一结果，
其中节点凭据来自公开 gist，随时可能失效。请自行在目标环境验证并遵守当地法律。
