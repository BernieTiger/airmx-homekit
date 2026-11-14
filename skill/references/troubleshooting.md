# 故障排查：按现象索引

排查原则：**先定位层级，再动手改**。四层拓扑决定了问题一定出在某一层。

```
L1 设备 → 路由器 DNS
L2 路由器 → gateway(25886)
L3 gateway → mqtt(1883) → bridge(25888)
L4 bridge → hap(51826) → 家庭 App
```

---

## 现象：设备一直不出现

**先查 L1**，90% 是 DNS。

```bash
dig +short i.airmx.cn
dig +short awm.airmx.cn
```

- 返回公网 IP → DNS 没生效。设备正在连已停运的服务器
- 返回你的内网 IP → 继续查 L2

```bash
# 看 gateway 有没有收到设备连接
docker compose -f deploy/compose.yaml logs gateway --tail 50 | grep -i "connect\|register"
```

**常见原因**

| 原因 | 判定 | 处理 |
|---|---|---|
| DNS 未生效 | `dig` 返回公网 | 改路由器 DNS 转发 |
| 设备缓存了旧 DNS | 改了 DNS 仍不出现 | 设备断电重启 |
| 设备与主机不同网段 | `ping` 不通 | 调整网络，或改 `AIRMX_DEVICE_CIDRS` |
| 网段白名单拦截 | gateway 日志有 `rejected` | 在 `.env` 里放行该网段 |
| 设备 Wi-Fi 灯快闪 | 现象本身 | 设备在重试连接，通常就是 DNS 问题 |

---

## 现象：家庭 App 显示「无响应」

**先查 L4**。这一步要分清是「hap 挂了」还是「HomeKit 映射坏了」。

```bash
# 1. hap 活着吗
nc -z <host> 51826 && echo alive

# 2. 服务端认为配件可用吗
TOKEN=$(grep AIRMX_BRIDGE_TOKEN deploy/.env | cut -d= -f2)
curl -s -H "Authorization: Bearer $TOKEN" localhost:51827/status | grep -o '"available":[a-z]*'
```

### 情况 A：`available: true`，但 App 还是无响应

说明服务端一切正常，问题在 **HomeKit 侧的映射**。

**最常见原因：服务的 IID 变了。**

HAP 规范要求同一配对下 IID 稳定。如果在配对之后**新增、删除、重排了服务**，
iOS 缓存的 IID → 特征映射就会失效，整个配件失联。

判定：
```bash
# 看服务的注册顺序
curl -s -H "Authorization: Bearer $TOKEN" localhost:51827/status | \
  python3 -c "import sys,json;print(json.load(sys.stdin)['accessories'][0]['services'])"
```

处理：**把服务结构改回配对时的样子**。改不回去就只能重新配对。

**改动服务结构前请想清楚：这一步值得让对方重新配对吗？**

### 情况 B：`available: false`

```bash
curl -s localhost:25888/devices/{id}/state
```

- `503 no_state_yet` → 设备还没上报过，等 60 秒
- `age_seconds > 200` → 设备离线或 bridge 没在轮询
- 请求失败 → bridge 挂了，看 `docker compose logs bridge`

---

## 现象：配件全丢，要重新配对

**先查状态文件是否可写。**

```bash
ls -l data/hap.state
```

配对的密钥存在这个文件里。它丢了 = 配对丢了。

**判定方法（关键）**：看 mDNS 广播里的 `id`（MAC）字段。

```bash
# macOS
dns-sd -B _hap._tcp local   # 记下名字，再用下面看详情
# 或用 python
python3 -c "
from zeroconf import Zeroconf, ServiceBrowser, ServiceListener
import time
class L(ServiceListener):
    def add_service(s,z,t,n):
        i=z.get_service_info(t,n)
        if i and i.port==51826: print(dict(i.properties))
    def update_service(s,*a): pass
    def remove_service(s,*a): pass
z=Zeroconf();ServiceBrowser(z,'_hap._tcp.local.',L());time.sleep(4);z.close()
"
```

- **MAC 每次都变** → 状态文件没写进去，重启就丢
- **MAC 稳定但 `sf=1`** → 文件可写但配对信息没了

处理：确认 `data/` 目录可写，且容器以 root 启动后能降权（代码里有 `_prepare_state_dir`）。

**血的教训**：不要在 `driver.async_start()` 之前调用 `config_changed()`。
它会用内存里的默认状态（含随机 MAC）覆盖磁盘上的配对信息。代码里已有回归测试守住这条。

---

## 现象：改了代码不生效

```bash
docker compose -f deploy/compose.yaml ps    # 看 STATUS 的启动时长
```

刚重启过才算生效。三种常见情况：

| 情况 | 判定 | 处理 |
|---|---|---|
| 容器没重启 | STATUS 显示很久 | `docker compose up -d --force-recreate hap` |
| 挂载没生效 | 改了文件但行为不变 | `docker compose exec hap head -1 /app/hap/accessory.py` |
| 用了旧镜像 | 重建后仍是旧代码 | `docker compose build --no-cache hap` |

---

## 现象：开关名字变成「开关 2 / 开关 3」

**这是 iOS 的行为，不是配置错误。**

判定：
```bash
curl -s -H "Authorization: Bearer $TOKEN" localhost:51827/status | \
  python3 -c "import sys,json;print([s['ConfiguredName'] for s in json.load(sys.stdin)['accessories'][0]['service_names']])"
```

如果返回的是「睡眠模式」「负离子」这些正确名字，说明**服务端没问题，是 iOS 没刷新**。

处理：**让配置版本号 +1**（重启一次 hap 即可），iOS 会重新拉取配件描述。

不要试图通过改代码解决——名字一直是对的。

---

## 现象：调了目标湿度，风扇不动

三层原因，逐层排除。

### 第一层：档位是「加湿」（手动）

手动档下风量固定，改目标湿度本来就不影响风量。这是正常行为。

```bash
curl -s localhost:25888/devices/{id}/state | grep -o '"mode":[0-9]'
```

`mode: 0` = 手动，`1` = 自动。

### 第二层：自动档下改目标，设备不重算

**这是设备固件的行为，实测结论：**

| 操作 | 设备反应 |
|---|---|
| 自动档下改目标湿度 | 只改数字，**不重算风量** |
| 原地重发 `mode=1`（1→1） | **完全无视** |
| 真正切换 `0→1` | 立刻重算 |

代码里的处理：改目标后等 12 秒（避开 bridge 节流），切手动，再等 12 秒切回自动。
整个过程约 25 秒，中间设备会短暂显示手动。

判定补偿有没有发出去：
```bash
curl -s -H "Authorization: Bearer $TOKEN" localhost:51827/status | \
  python3 -c "import sys,json;[print(c) for c in json.load(sys.stdin)['accessories'][0]['recent_calls']]"
```

应该看到 `控制 {"mode":0} 重算补偿·切手动` 和 `{"mode":1} 重算补偿·切回自动`。

### 第三层：降速比升速慢

实测：升速 10–15 秒，降速最多 45 秒。**等一分钟再判断**，不要看到 100% 就认为没生效。

---

## 现象：Siri 控制不了

`recent_calls` 里没有「入站」记录 = 指令根本没到 hap。

```bash
curl -s -H "Authorization: Bearer $TOKEN" localhost:51827/status | \
  python3 -c "import sys,json;[print(c) for c in json.load(sys.stdin)['accessories'][0]['recent_calls']]"
```

- **没有任何「入站」记录** → Siri 没匹配到这个配件。换个说法，带上完整名字
- **有入站但没下发** → 看下游记录，或是 bridge 节流丢了

bridge 对同一设备的写指令有约 10 秒节流，短时间连续操作会丢后一条。

---

## 通用手法：看 `recent_calls`

`/status` 里的 `recent_calls` 是排查写入类问题最有效的手段，记录了最近 12 次**入站与出站**。

```json
{"时间":"19:04:04","类型":"入站","内容":{"setter":"TargetHumidity","value":70}}
{"时间":"19:04:04","类型":"settings","内容":{"hThreshold":70},"备注":"成功"}
{"时间":"19:04:16","类型":"control","内容":{"mode":0},"备注":"重算补偿·切手动"}
```

三段一眼看出：HomeKit 有没有发 → 我们有没有转 → 设备收没收到。
