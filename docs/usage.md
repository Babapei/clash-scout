# scout 使用说明

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

## 自动记录（可选，推荐）

不想每次手动跑？配一个定时任务，每 10 分钟自动记录一次。

```bash
bash setup-scheduler.sh           # 默认每 10 分钟
bash setup-scheduler.sh 30        # 改成每 30 分钟
bash setup-scheduler.sh --remove  # 卸载
```

它做三件事：

1. 检查 Clash Verge 是否在运行（不在就跳过，不报错）
2. 跑 `scout --from-clash check`
3. 结果写进表，同时记日志

看日志：

```bash
tail -f ~/.clash-ip-tracker/scheduler.log
```

**macOS 注意**：launchd 不能访问 `~/Documents`（系统隐私保护），
脚本会自动把运行文件复制到 `~/.clash-ip-tracker/app/` 再跑，项目本身不用移动。

**Linux 用户**可以自己配 cron：

```cron
*/10 * * * * cd /path/to/clash-ip-tracker && python3 -m clash_ip_tracker --from-clash check
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
