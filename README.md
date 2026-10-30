# clash-ip-tracker

记录代理节点域名背后的 IP 池健康状态，找出**当前最值得手动指定**的 IP。

> 纯只读工具：只发 DNS 查询和 TCP 握手，**不修改任何 Clash 配置、不碰系统文件**。

---

## 它解决什么问题

机场常用一个域名配多个 A 记录（如 `n43ds.chhbuv.cn`），DNS 每次返回的 IP 可能不同。这些 IP 的状态差异很大：

- 有些一直健康
- 有些被网络环境（校园网/公司网）针对性封锁
- 有些是机场自己就没部署服务的死 IP

结果就是「代理时好时坏」，但**你无法知道当前用的是好 IP 还是坏 IP**，因为 Clash 不保存历史。

这个工具做一件事：**持续把观察到的 IP 和它们的健康状态记下来，形成一张可查的表。**

---

## 安装

无需第三方依赖，Python 3.9+ 即可。

```bash
cd clash-ip-tracker
python3 -m pytest tests/ -v        # 可选：跑测试
```

直接运行（不安装）：

```bash
python3 -m clash_ip_tracker --domain <节点域名> --port <端口> check
```

或安装成命令：

```bash
pip3 install -e .
clash-ip-tracker --domain <域名> --port <端口> check
```

---

## 用法

### 常用：采样 + 探测 + 出报告

```bash
python3 -m clash_ip_tracker --domain n43ds.chhbuv.cn --port 61750 check
```

输出示例：

```
  探测 209.248.40.46    OK   156ms
  探测 209.248.40.47    DEAD  -
  探测 47.131.82.134    OK   81ms

域名: n43ds.chhbuv.cn    端口: 61750
本轮 DNS 采样发现 4 个 IP，表内共 4 个
状态统计: 活 3 / 时好时坏 1 / 死 0 / 陈旧 0

IP               状态       成功/失败      成功率     延迟       最后检测         最后可用
-------------------------------------------------------------------------------
209.248.40.46    alive    1/0        100%    156ms    10-30 21:12  10-30 21:12
47.131.82.134    alive    1/0        100%    81ms     10-30 21:12  10-30 21:12
209.248.40.47    flaky    0/1        0%      -        10-30 21:12  -

推荐 IP: 47.131.82.134  (成功率 100%，延迟 80.8ms)
```

### 其它子命令

```bash
# 只采样 DNS，不做端口探测（最轻）
python3 -m clash_ip_tracker --domain <域名> sample

# 只看表，完全不联网
python3 -m clash_ip_tracker --domain <域名> report

# 只输出推荐 IP（给脚本用）
python3 -m clash_ip_tracker --domain <域名> best
```

---

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
~/.clash-ip-tracker/table.json     # 表
~/.clash-ip-tracker/table.json.lock # 文件锁
```

按域名分组，可以同时记录多个机场。

```json
{
  "version": 1,
  "domains": {
    "n43ds.chhbuv.cn": {
      "ports": [61750],
      "ips": {
        "47.131.82.134": {
          "first_seen": "2026-10-30T21:12:00+08:00",
          "last_seen": "2026-10-30T21:12:00+08:00",
          "last_check": "2026-10-30T21:12:00+08:00",
          "last_ok": "2026-10-30T21:12:00+08:00",
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
```

---

## 配置

默认值写在 `clash_ip_tracker/config.py`，可通过 `~/.clash-ip-tracker/config.json` 覆盖：

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

也可用环境变量临时覆盖：`CLASH_TRACKER_DNS`、`CLASH_TRACKER_PORTS`、`CLASH_TRACKER_DATA`。

---

## 关于「会不会被拉黑」

设计上刻意保持低频率、低特征：

| 行为 | 频率 | 说明 |
|---|---|---|
| DNS 采样 | 每 30 分钟一次 | 每次只查几轮 |
| TCP 探测 | 每 5~15 分钟 | 只对「距上次检测超过 N 分钟」的 IP |
| 探测方式 | TCP 三次握手 | 不发业务数据，握手完立即关闭 |

量级参考：15 个 IP × 每 10 分钟一次 ≈ **每小时 90 次握手**，远低于正常浏览网页产生的连接数。

**不建议**：秒级监控、全端口扫描、并发轰炸。这些才会触发防护。

---

## 局限（重要）

1. **看不到完整 IP 池**：只能通过多次 DNS 采样「尽量采样全」，永远可能漏。
2. **不能自动修复**：Verge 没有公开的重载接口，所以工具只「记录 + 推荐」，切换需要手动。
3. **探测成功 ≠ 能上网**：TCP 通只代表端口开着；节点可能仍然拒绝服务。
4. **不解决根本问题**：如果机场整体质量差或被针对性封锁，工具只能帮你选好一点的 IP。

---

## 扩展方向

- [ ] 自动从 Clash/Verge 配置读取当前订阅的域名和端口
- [ ] 支持 TLS 握手探测（已在 `port_probe.py` 预留）
- [ ] 导出 CSV
- [ ] 按小时统计成功率曲线，观察池子是否在恶化
- [ ] 生成 `hosts` / Verge Script 片段（仍需手动应用）
- [ ] 多机场并行采样

---

## 项目结构

```
clash_ip_tracker/
├── config.py      # 默认配置 + 覆盖
├── store.py       # JSON 表、文件锁、状态机
├── dns_probe.py   # DNS 采样（调 dig）
├── port_probe.py  # TCP / TLS 探测
├── report.py      # 表格渲染 + 推荐逻辑
└── cli.py         # 命令行
tests/
└── test_store.py  # 状态机与存储的单测
```

---

## License

本地自用工具，未指定许可证。
