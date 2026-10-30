# scout

这个工具是因为一个具体问题做的：**在家用代理正常，一到学校就连不上。**

排查下来是两件事叠加——**校园网封了一部分节点 IP**（换到家里或手机热点就好了），
而**机场自己也有一批死 IP**（在哪儿都不通）。同一个域名每次解析出来的 IP 不一样，
所以表现出来就是「时好时坏」，光靠感觉分不清是哪一种。

Clash 不保存这些历史，也不告诉你现在连的是哪个 IP、好还是坏。scout 就是给它配一个
「病历本」：按网络环境（家 / 学校 / 热点）记录每个 IP 的状态，卡的时候能查出该换成哪个。

想做到三步：**① 记录 + 找出能用的**（已做）、**② 一键固定到 Verge**（手动能做，自动化待补）、
**③ IP 失效时自动换**（规划中）。

> 纯只读：只发 DNS 查询和 TCP 握手，不改任何 Clash 配置、不碰系统文件。

---

## 安装

需要 Python 3.9+、系统自带的 `dig`，以及 PyYAML（macOS 系统 Python 通常已带）。

**方式一：pip 安装（推荐，之后直接敲 `scout`）**

```bash
cd clash-ip-tracker
python3 -m pip install --user -e . --no-build-isolation
```

> `--no-build-isolation` 是为了避免联网下载 setuptools（国内常连不上 PyPI）。
> 装完如果 `scout` 找不到，把它软链到已在 PATH 的目录：
>
> ```bash
> ln -sf ~/Library/Python/3.9/bin/scout ~/.local/bin/scout
> ```

**方式二：不安装，直接跑**

```bash
python3 scout.py --domain <域名> --port <端口> check
```

跑测试：

```bash
python3 -m unittest discover -s tests -v
```

---

## 用法

### 核心：自动识别网络环境

不指定 `--env` 时，用**默认网关 IP** 自动区分网络：

```bash
# 在家里（网关 192.168.30.1）→ 自动记到 gw:192.168.30.1
scout --domain n43ds.chhbuv.cn --port 61750 check

# 在学校（网关 172.27.255.254）→ 自动记到 gw:172.27.255.254
scout --domain n43ds.chhbuv.cn --port 61750 check
```

也可以手动命名：

```bash
scout --domain <域名> --env home check
scout --domain <域名> --env campus check
scout --domain <域名> --env hotspot check
```

### 采样 + 探测 + 报告

```bash
# 自动从 Verge 读当前订阅的域名和端口（推荐，省得手输）
scout --from-clash --env hotspot check

# 或者手动指定
scout --domain n43ds.chhbuv.cn --port 61750 --env hotspot check
```

> **注意端口**：同一个订阅里，不同节点可能用不同端口（如 `28537`、`61750`），
> 端口的可用性可能完全不同。用 `--from-clash` 会自动选出现最多的那个端口。

```
环境 hotspot：探测 2 个 IP
  15.168.19.83     OK    154ms
  209.248.40.49    OK    205ms

环境: hotspot    域名: n43ds.chhbuv.cn    端口: 61750
本轮 DNS 采样发现 2 个 IP，本环境共记录 2 个
状态统计: 活 2 / 时好时坏 0 / 死 0 / 陈旧 0

IP               状态       成功/失败      成功率     延迟       最后检测         最后可用
-------------------------------------------------------------------------------
15.168.19.83     alive    1/0        100%    154ms    10-30 21:19  10-30 21:19
209.248.40.49    alive    1/0        100%    205ms    10-30 21:19  10-30 21:19

推荐 IP: 15.168.19.83  (成功率 100%，延迟 153.6ms)
```

### 看当前用的是哪个 IP（最实用的诊断）

```bash
scout --from-clash current
```

```
从 Verge 读取：订阅「红杏云」，选中节点「🇯🇵 日本原生」
  → n43ds.chhbuv.cn:61750
环境: gw:192.168.30.1    端口: 61750
Clash 当前建立了 1 个连接，对端 IP：

  209.248.40.49    新 IP → 实时探测 OK  187ms

结论：当前连的是健康 IP。如果上网还卡，问题不在 IP，去查 WiFi / 线路。
```

**这是排查“卡了到底是 IP 问题还是 WiFi 问题”最快的方法**：

| 结论 | 说明 |
|---|---|
| 当前 IP 健康 + 上网卡 | **不是 IP 问题**，查 WiFi / 线路 |
| 当前 IP 不健康 | IP 问题，重载配置重新解析 |

表里没记录的 IP 会当场探测并记进表，不会“查不到就跳过”。

### 跨环境对比（这个最有价值）

```bash
scout --domain n43ds.chhbuv.cn compare
```

```
域名: n43ds.chhbuv.cn    对比 3 个环境

IP               home       campus     hotspot    结论
------------------------------------------------------------
47.131.82.134    OK         OK         OK         健康
209.248.40.46    OK         X          OK         部分环境被封
209.248.40.42    X          X          X          机场死 IP
```

**一眼看出每个 IP 是被网络环境干掉的，还是机场自己就没服务。**

### 其它子命令

```bash
# 只采样 DNS，不做端口探测（最轻）
scout --domain <域名> sample

# 只看表，完全不联网
scout --domain <域名> report

# 只输出推荐 IP（给脚本用）
scout --domain <域名> best

# 列出所有环境
scout --domain <域名> compare

# 列出表里所有机场域名和各自的健康概况（换过机场后一起看）
scout airports
```

---

## 找到稳定的 IP 之后怎么用

工具只负责「找出哪个 IP 最稳」，**不会自动帮你切换**（Verge 没有可编程的重载接口）。

要把它固定下来用，两种手动方式：

**方式一：Verge 的 DNS Hosts（推荐，不用碰系统）**

```
设置 → Clash 设置 → DNS 设置 → Hosts 一栏填：

n43ds.chhbuv.cn=47.131.82.134
```

保存后重启 Verge。这样域名固定解析到这个 IP，不再靠 DNS 抽签。

**方式二：Script 扩展**

写一个 JS 脚本，把节点的 `server` 字段改成 IP：

```js
function main(config) {
  const GOOD_IP = '47.131.82.134'
  config.proxies.forEach(p => {
    if (p.server === 'n43ds.chhbuv.cn') p.server = GOOD_IP
  })
  return config
}
```

**重要提醒：IP 会失效**

今天实测的寿命是**几十分钟到几小时**。失效后：

```bash
scout --from-clash check     # 重新采样，拿到新的推荐 IP
```

然后把 hosts / Script 里的 IP 换成新的。

**所以这不是「设一次永久有效」**，而是「卡的时候查一下，换一下」。工具的价值在于**让你知道该换成哪个**，而不是替你还原。

## 状态含义

| 状态 | 含义 |
|---|---|
| `unknown` | 刚发现，还没探测过 |
| `alive` | 最近一次探测成功 |
| `flaky` | 时好时坏（一次失败，或失败后恢复） |
| `dead` | 连续 2 次失败 |
| `stale` | 超过 N 天没在 DNS 里出现（保留历史，不删除） |

**推荐逻辑**：`alive` 优先于 `flaky`，再看成功率，最后看延迟。

---

## 数据位置

```
~/.clash-ip-tracker/table.json      # 表
~/.clash-ip-tracker/table.json.lock # 文件锁
```

按「环境 → 域名 → IP」三层组织：

```json
{
  "version": 2,
  "environments": {
    "gw:192.168.30.1": {
      "label": "gw:192.168.30.1",
      "last_seen": "2026-10-30T21:19:00+08:00",
      "domains": {
        "n43ds.chhbuv.cn": {
          "ports": [61750],
          "ips": {
            "47.131.82.134": {
              "first_seen": "...",
              "last_seen": "...",
              "last_check": "...",
              "last_ok": "...",
              "status": "alive",
              "ok": 1,
              "fail": 0,
              "consecutive_fail": 0,
              "latency_ms": 81,
              "stale": false
            }
          }
        }
      }
    }
  }
}
```

旧版（v1，顶层 `domains`）会自动迁移到 `unknown` 环境。

---

## 配置

默认值在 `clash_ip_tracker/config.py`，可用 `~/.clash-ip-tracker/config.json` 覆盖：

```json
{
  "dns_servers": ["223.5.5.5", "114.114.114.114"],
  "dns_samples": 6,
  "dns_gap_seconds": 0.3,
  "probe_ports": [61750],
  "probe_timeout": 3.0,
  "recheck_minutes": 10,
  "stale_days": 7
}
```

环境变量：`CLASH_TRACKER_DNS`、`CLASH_TRACKER_PORTS`、`CLASH_TRACKER_DATA`。

---

## 关于「会不会被拉黑」

设计上刻意保持低频率、低特征：

| 行为 | 频率 | 说明 |
|---|---|---|
| DNS 采样 | 每 30 分钟 | 每次只查几轮 |
| TCP 探测 | 每 5~15 分钟 | 只对「距上次检测超过 N 分钟」的 IP |
| 探测方式 | TCP 三次握手 | 不发业务数据，握手完立即关闭 |

量级参考：15 个 IP × 每 10 分钟一次 ≈ **每小时 90 次握手**，远低于正常浏览产生的连接数。

**不建议**：秒级监控、全端口扫描、并发轰炸。

---

## 当前边界

这些是「现在还没做的」，不是「做不到的」：

1. **完整 IP 池**：只能反复采样逼近。下一步可以拉长采样窗口、接入多个 DNS 源。
2. **自动切换**：现在是手动改 hosts / Script。下一步找 Verge 的可编程入口（IPC、文件监听、或包装重启）。
3. **探测深度**：现在只测 TCP 握手。下一步加 TLS 握手和真实 HTTP 请求，区分「端口开着」和「真能上网」。
4. **根本质量**：如果机场整体差，工具只能帮你选相对好的——但「选得准」本身就能省掉大量盲试。

---

## 扩展方向

- [ ] 自动从 Clash/Verge 配置读取当前订阅的域名和端口
- [ ] 支持 TLS 握手探测（`port_probe.py` 已预留）
- [ ] 导出 CSV
- [ ] 按小时统计成功率曲线，观察池子是否恶化
- [ ] 生成 `hosts` / Verge Script 片段（仍需手动应用）
- [ ] 多机场并行采样

---

## 项目结构

```
clash_ip_tracker/
├── config.py      # 默认配置 + 覆盖
├── store.py       # JSON 表、文件锁、状态机、v1→v2 迁移
├── dns_probe.py   # DNS 采样（调 dig）
├── port_probe.py  # TCP / TLS 探测
├── report.py      # 表格渲染 + 跨环境对比 + 推荐逻辑
└── cli.py         # 命令行
tests/
└── test_store.py  # 12 个单测：状态机、环境隔离、迁移、stale
```

---

## License

本地自用工具，未指定许可证。
