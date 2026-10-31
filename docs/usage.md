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
> **域名和端口是自动读的**：默认从 Clash Verge 的当前订阅读取，
> 不用加 `--from-clash`。想手动指定就用 `--domain` / `--port`。

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
scout --env hotspot check

# 或者手动指定
scout --domain n43ds.chhbuv.cn --port 61750 --env hotspot check
```

> **注意端口**：同一个订阅里，不同节点可能用不同端口（如 `28537`、`61750`），
> 端口的可用性可能完全不同。默认会从 Verge 读「当前选中节点」的端口。

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
scout current
```

```
从 Verge 读取：订阅「红杏云」，选中节点「🇯🇵 日本原生」
  → n43ds.chhbuv.cn:61750
环境: gw:192.168.30.1    端口: 61750
Clash 当前建立了 1 个连接，对端 IP：

  209.248.40.49    新 IP → 实时探测 OK  187ms

结论：当前连的是健康 IP。如果上网还卡，问题不在 IP，去查 WiFi / 线路。
```

**手动 vs 自动的范围区别**：

| 命令 | 测哪些 |
|---|---|
| `scout check` | 当前机场 |
| `scout check --all` | **所有机场**（不看勾选） |
| `scout check --airport X` | 指定的 |
| 自动任务 | **只测勾选的** |

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

**数据年龄标记**：超过 1 小时的数据会标出来。

| 数据年龄 | 显示 |
|---|---|
| 1 小时内 | `OK`（默认视为新鲜） |
| 1 小时 ~ 1 天 | `OK (3h前)` |
| 超过 1 天 | `OK (2d前)` |

compare 比的是各环境「最后一次记录」，时间可能差很远（你不可能同时在两个网络）。
标出年龄才知道结论可不可信。

要精确时间用 `--time`：

```bash
scout compare --time
```

```
IP        家                学校              结论
1.1.1.1   OK (10-30 22:25)  X (10-30 16:28)  部分环境被封
```

### 其它子命令

```bash
# 测所有机场，不管有没有勾选（不影响当前订阅）
scout check --all

# 只测指定机场（用别名或域名）
scout check --airport 红杏云
scout check --airport n43ds.chhbuv.cn

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

# 删除不要的记录（环境 / IP / 域名）
scout forget --env hotspot
scout forget --ip 1.2.3.4
scout forget --domain old-airport.com

# 给环境 / 机场起别名（只影响显示）
scout env 家
scout airport 红杏云
scout alias

# 打开终端界面（需要先装 textual）
scout ui
```

### 别名（`scout env` / `scout airport`）

网络和机场多了以后，`gw:192.168.30.1`、`n43ds.chhbuv.cn` 不好记，可以起别名：

```bash
scout env 家                  # 给当前网络起名
scout airport 红杏云           # 给当前机场起名
scout alias                  # 列出所有别名

scout airport 红杏云 --auto off   # 这个机场不参与自动检测
scout airport 红杏云 --auto on    # 加回来

scout env --clear             # 当前环境恢复原始显示
scout airport --clear         # 当前机场恢复原始显示
```

不填名字时只显示当前值：

```bash
scout env                    # 当前环境: gw:192.168.30.1 → 显示为「家」
scout airport                # 当前机场: n43ds.chhbuv.cn → 显示为「红杏云」
```

**内部标识不变，只改显示**：

```
内部（匹配/存储用）      显示（给人看）
gw:192.168.30.1    →    家
n43ds.chhbuv.cn    →    红杏云
```

所以：

- 改别名**不会**导致数据错乱
- 自动识别还是按网关 / 域名
- 如果换了路由器（网关变），会变成新环境，旧数据保留在旧 id 下

### 终端界面 `scout ui`

```bash
python3 -m pip install textual -i https://pypi.tuna.tsinghua.edu.cn/simple
scout ui
```

界面分三个 Tab：

| Tab | 内容 | 快捷键 |
|---|---|---|
| **当前** | 当前网络 + 当前机场的 IP 明细 | `1` |
| **环境对比** | 同一域名在不同网络下的状态 | `2` |
| **机场** | 按域名一行：所属机场、端口、各环境活/抖/死、唯一IP、自动开关。会合并 Verge 里所有订阅，没采集过的显示「未采集」 | `3` |
| **别名** | 内部标识 ↔ 显示名，可直接改名 | `4` |
| **设置** | 自动任务开关、检测间隔 | `5` |

按键：

| 按键 | 作用 |
|---|---|
| `1` ~ `5` | 切换 Tab |
| `e` | 在别名 Tab 里改选中项的名字 |
| `空格` | 在机场 Tab 里切换是否参与自动检测（未采集的机场也能切） |
| `a` | 开启 / 关闭自动任务 |
| `+` / `-` | 检测间隔 +5 / -5 分钟 |
| `r` | 刷新 |
| `c` | 检查一轮（探测所有 IP） |
| `q` | 退出 |

**别名 Tab 里改名**：选中一行按 `e`，弹窗里输入新名字回车保存；留空回车 = 恢复原始值（`gw:...` / 域名）。命令行 `scout env` / `scout airport` 依然可用。

当前正在连接的 IP 前面标 `●`。每 30 秒自动刷新一次。

---

## 自动记录（可选，推荐）

不想每次手动跑？配一个定时任务，每 10 分钟自动记录一次。

这是内建命令，`scout --help` 里能看到。

```bash
scout auto               # 查看状态（范围、间隔、上次运行时间）
scout auto on            # 开启：测所有勾选的机场
scout auto on --every 30 # 改成每 30 分钟
scout auto off           # 关闭

# 测哪些由机场的勾选决定
scout airport 红杏云 --auto off   # 取消勾选，不测它
scout airport 红杏云 --auto on    # 勾上
scout airport --id 域名 --auto off  # 用 --id 指定还没采集过的机场
```

状态输出示例：

```
自动记录：已开启
  状态：已加载
  间隔：每 10 分钟
  上次运行：2026-10-31 00:49:10 开始记录
  范围：所有勾选的机场
  日志：/Users/xxx/.clash-ip-tracker/scheduler.log
  关闭：scout auto off
```

它做三件事：

1. 检查 Clash Verge 是否在运行（不在就跳过，不报错）
2. 跑 `scout check`
3. 结果写进表，同时记日志

看日志：

```bash
tail -f ~/.clash-ip-tracker/scheduler.log
```

**macOS 注意**：launchd 不能访问 `~/Documents`（系统隐私保护），
脚本会自动把运行文件复制到 `~/.clash-ip-tracker/app/` 再跑，项目本身不用移动。

> **改了代码之后要重跑一次 `scout auto on`**，否则自动任务还在用旧的运行副本。

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
scout check     # 重新采样，拿到新的推荐 IP
```

然后把 hosts / Script 里的 IP 换成新的。

**所以这不是「设一次永久有效」**，而是「卡的时候查一下，换一下」。工具的价值在于**让你知道该换成哪个**，而不是替你还原。

## 域名变更

机场偶尔会换入口域名（同一个订阅内），比如：

```
n43ds.chhbuv.cn  →  anquan43ds.chhbuv.cn
```

**旧域名的 IP 池可能整体失效**，表现是「Verge 里测速全 error」。

### scout 怎么处理

**① 检测并提示 + 自动继承别名**

每次 `check` 会对比：

```
⚠️ 检测到域名变更（同一订阅）：n43ds.chhbuv.cn → anquan43ds.chhbuv.cn
   旧域名的数据保留，但可能已失效。
```

新域名会自动继承旧域名的显示名，不用手动再设。

**② 按订阅分组查看**

```bash
scout airports
```

```
机场 红杏云
  环境 校园网
    anquan43ds.chhbuv.cn   活 5   抖 0   死 0    ← 新，健康
    n43ds.chhbuv.cn        活 0   抖 1   死 6    ← 旧，已失效
  环境 寝室
    anquan43ds.chhbuv.cn   活 4   抖 1   死 0
    n43ds.chhbuv.cn        活 0   抖 0   死 7
```

**新旧域名并排，一眼看出哪个还活着。**

### 为什么会这样

机场为了抗封锁，会定期更换入口域名。旧的域名不一定马上撤掉，但背后的 IP 会陆续失效。

**遇到全 error 时，第一件事是更新订阅**（Verge → 订阅 → 右键 → 更新）。

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
