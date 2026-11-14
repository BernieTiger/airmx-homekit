---
name: airmx-homekit
description: 把秒新 AirMX 加湿器（AirWater A3/A3S/A5）接入 Apple 家庭。适用于厂商已停服、官方 App 失效、需要用本地网关恢复远程控制与自动化的场景。当用户提到"秒新"、"AirMX"、"AirWater"、"加湿器接入 HomeKit"、"官方 App 打不开"时使用。提供环境勘察、部署决策树、逐步验收标准与故障排查路径。
---

# 秒新加湿器接入 HomeKit

## 这个 Skill 解决什么问题

秒新（AirMX）已停止运营，官方服务器关停。用户手里的加湿器硬件完好，但：
- App 无法登录，云端指令通道消失
- 只剩机身按键，无法远程、无法自动化

本 Skill 指导你把这条链路整体搬到本地：设备 → 本地网关 → bridge → HomeKit 桥接 → 家庭 App。

**核心原理**：设备会去连 `*.airmx.cn`。只要让这个域名解析到你的机器，设备就以为还在连官方服务器。

---

## 使用前必须完成的勘察

**不要跳过这一步。** 下面 6 项任意一项不满足，后面的步骤都会失败。

| # | 检查项 | 命令 | 期望结果 | 不满足时 |
|---|---|---|---|---|
| 1 | Docker 可用 | `docker --version && docker compose version` | 两个都有版本号 | 停下来，先装 Docker |
| 2 | 有一台常开设备 | 询问用户 | NAS / 树莓派 / 常开主机 | 手机和笔记本不行，DNS 会断 |
| 3 | 51826 未被占用 | `nc -z <host> 51826` | 无输出即空闲 | 改 `.env` 的 `AIRMX_HAP_PORT` |
| 4 | 25886/25888/1883 未被占用 | 同上逐个测 | 空闲 | 改 `.env` 对应端口 |
| 5 | 能改路由器 DNS | 询问用户 | 能进后台改 DNS 转发 | **无法继续**，这是硬前提 |
| 6 | 设备与主机同网段 | `ping <设备IP>` | 能通 | 先解决网络分层 |

把这 6 项的结果列成表格给用户看，再继续。

---

## 决策分支

```
有 Docker？
├─ 有 → 有常开的 Linux/NAS？
│        ├─ 有 → 标准路径（下面第一节）
│        └─ 没有 → 告知用户：需要一个常开设备，否则 DNS 断了设备就失联
└─ 没有 → 装 Docker，或改用 venv 路径（见 references/deploy.md 末尾）
```

**不要**在没有常开设备的情况下强行部署。笔记本合盖 = DNS 失效 = 设备连不上云端也连不上本地，表现为 Wi-Fi 灯快闪。

---

## 部署顺序（每步都要验收）

### 步骤 1：获取代码

```bash
git clone https://github.com/<repo>/airmx-homekit.git
cd airmx-homekit
```

**验收**：`ls hap/ airmx_gateway/ bridge/` 三个目录都存在。

---

### 步骤 2：生成配置

```bash
./scripts/bootstrap.sh
```

**验收**：
- `secrets/admin_token` 存在，32 位十六进制
- `deploy/.env` 存在，`AIRMX_BRIDGE_TOKEN` 非空

**失败分支**：
- 报 "未找到 docker compose" → 装 compose 插件，不要用 `docker-compose`（v1 已废弃）
- 报权限错误 → `chmod +x scripts/bootstrap.sh`

**禁止**：不要把 token 直接写进 `compose.yaml`。它会随仓库泄露。

---

### 步骤 3：启动服务

```bash
docker compose -f deploy/compose.yaml up -d
docker compose -f deploy/compose.yaml ps
```

**验收**：四个服务 `gateway / mqtt / bridge / hap` 都是 `running`。

```bash
curl -s localhost:25888/health
```

**期望**：`{"mqtt_connected": true, "device_count": N, ...}`

**失败分支**：
- 端口占用 → 改 `.env` 端口后重来
- `mqtt_connected: false` → mqtt 容器没起来，看 `docker compose logs mqtt`
- compose 报 "AIRMX_BRIDGE_TOKEN is required" → 没跑 bootstrap

---

### 步骤 4：配置 DNS（最容易被跳过的一步）

在路由器上把这两条指向本机 IP：

```
i.airmx.cn    → <本机IP>
awm.airmx.cn  → <本机IP>
```

**验收**（在局域网内另一台机器上）：

```bash
dig +short i.airmx.cn
```

**期望**：返回你的本机 IP，而不是公网地址。

**失败分支**：
- 返回公网 IP → DNS 没生效，设备会去连已停运的服务器，永远不出现
- 设备 Wi-Fi 灯快闪 → 同上，或设备缓存了旧 DNS，断电重启设备

**禁止**：不要用 hosts 文件代替。设备不读你的 hosts。

---

### 步骤 5：配对

家庭 App → 右上角 `+` → 添加配件 → 输入 `deploy/.env` 里的 `AIRMX_HAP_PINCODE`。

**验收**：

```bash
curl -s -H "Authorization: Bearer $(grep AIRMX_BRIDGE_TOKEN deploy/.env | cut -d= -f2)" \
     localhost:51827/status | head -c 200
```

**期望**：返回 JSON，`paired_clients` ≥ 1，`accessories` 里有设备。

**失败分支**：
- App 搜不到配件 → mDNS 出不去，确认 hap 用了 `network_mode: host`
- 搜到但配对失败 → 检查配对码格式 `xxx-xx-xx`
- 配对后显示"无响应" → 读 `docs/04-故障排查.md` 的"无响应"一节

---

## 验证清单（部署完成后逐条确认）

| 项 | 命令 | 期望 |
|---|---|---|
| 设备在线 | `curl -s localhost:25888/devices` | 每台 `online: true` |
| 有密钥 | 同上 | `has_key: true` |
| 桥接正常 | `curl -s localhost:25888/health` | `mqtt_connected: true` |
| 配件可用 | `curl -s -H "Authorization: Bearer $TOKEN" localhost:51827/status` | `available: true` |
| 已配对 | 同上 | `paired_clients` ≥ 1 |
| 能控制 | 家庭 App 里开关一次 | 设备有反应 |

六项全过才算部署成功。**不要凭"App 里能看到图标"就判定成功。**

---

## 遇到问题

按现象查 `references/troubleshooting.md`，不要凭猜。

最常见的三个坑：

1. **设备一直不出现** → DNS 没生效（90% 是这个）
2. **配对后"无响应"** → 状态文件写不进去，或 IID 变了
3. **改了代码不生效** → 容器没真重启，或挂载没生效

---

## 边界与禁止事项

**可以做**
- 修改 `hap/profiles.py` 适配新机型
- 调整 `hap/mapping.py` 里的映射关系
- 改 UI 档位（`AIRMX_HAP_PROFILE`）

**不要做**
- 不要改设备固件、不要刷机。本方案一个字节都不改设备
- 不要把 51826 / 25888 暴露到公网。这等于把加湿器的控制权公开
- 不要把 token、`hap.state`、`secrets/` 提交到 Git
- 不要在 Issue 里粘贴含内网 IP、设备 ID 的日志
- 不要用公网中继代替局域网。设备协议没有加密，公网传输等于裸奔

**能力边界（先告诉用户，避免期待落空）**

| 需求 | 能否实现 | 原因 |
|---|---|---|
| 水位数字 | ❌ Home App 里没有 | HAP 无水位服务，iOS 不渲染 FilterMaintenance |
| 自动档下调目标湿度 | ⚠️ 需先切回「加湿」 | iOS 在自动档隐藏目标湿度滑块 |
| 外网控制 | ❌ 不在本项目范围 | 需要用户自己做内网穿透，且不推荐 |
| 多档模式（M/50/45/40/睡眠） | ⚠️ 部分 | 面板的 50/45/40 是目标湿度预设，不是独立模式 |
