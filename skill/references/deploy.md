# 部署细节

`SKILL.md` 里是主干流程，这里补不同环境的差异与命令细节。

---

## 路径 A：群晖 NAS（Container Manager）

最省事的部署目标——机器常开、有固定内网 IP、Docker 现成。

### 1. 准备目录

DSM 的 File Station 里建一个共享文件夹，例如 `docker/airmx-homekit`，把仓库放进去。

或直接 SSH：

```bash
cd /volume1/docker          # 换成你的共享文件夹路径
git clone https://github.com/<repo>/airmx-homekit.git
cd airmx-homekit
```

### 2. 生成配置

```bash
./scripts/bootstrap.sh
```

### 3. 启动

**不要用 Container Manager 的「项目 → 构建」**。实测在 DSM 7.2 上会卡死在构建阶段，
且容器详情页会出现 `容器 undefined 不存在` 的错误，日志也打不开。

正确做法是用计划任务执行 compose 命令：

```
控制面板 → 任务计划 → 新增 → 用户定义的脚本
  用户：root
  命令：cd <共享文件夹>/airmx-homekit && docker compose -f deploy/compose.yaml up -d
```

建好后可随时在任务列表里手动「运行」。

### 4. 端口检查

```bash
for p in 25886 25887 1883 25888 51826 51827; do
  nc -z 127.0.0.1 $p && echo "$p ok" || echo "$p 未监听"
done
```

`51827` 是只读状态接口，不监听不影响功能，但排查时很有用。

---

## 路径 B：树莓派 / 普通 Linux

```bash
sudo apt install -y docker.io docker-compose-plugin
sudo usermod -aG docker $USER   # 重新登录生效

git clone https://github.com/<repo>/airmx-homekit.git
cd airmx-homekit
./scripts/bootstrap.sh
docker compose -f deploy/compose.yaml up -d
```

注意：`hap` 用 `network_mode: host`，Linux 上没问题；**macOS 上 Docker Desktop 不支持 host 网络**，
mDNS 出不去，家庭 App 发现不了配件。所以不要在 Mac 上跑正式部署。

---

## 路径 C：不用 Docker（venv）

适合已经有 Python 环境、不想装 Docker 的场景。三个进程要分别常驻。

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r hap/requirements.txt
```

**需要三个常驻进程**（用 systemd 或 supervisor 管理）：

```bash
# 1. 网关
python -m airmx_gateway

# 2. bridge
BRIDGE_API_TOKEN=<token> python bridge/bridge.py

# 3. HomeKit 桥接
AIRMX_BRIDGE_URL=http://127.0.0.1:25888 \
AIRMX_BRIDGE_TOKEN=<token> \
AIRMX_HAP_STATE=$PWD/data/hap.state \
python -m hap
```

还需要本地跑一个 MQTT broker（mosquitto），配置见 `deploy/mosquitto.conf`。

**不推荐新手走这条路**——三个进程的依赖顺序、环境变量、日志分散，排查成本高。

---

## 路由器 DNS 配置

不同品牌位置不同，找「DNS 转发」「本地 DNS」「Hosts」这类功能。

需要的两条记录：

```
i.airmx.cn    → <本机IP>
awm.airmx.cn  → <本机IP>
```

### 为什么是这两条

设备启动后会去解析这两个域名，分别用于业务通信和固件更新。
把它们指向本地，设备就会把本地网关当作官方服务器。

### 验证

在**另一台**局域网设备上：

```bash
dig +short i.airmx.cn    @<路由器IP>
nslookup awm.airmx.cn    <路由器IP>
```

返回本机 IP 才算成功。在本机测没意义（可能是缓存）。

### 常见失败

| 现象 | 原因 |
|---|---|
| 返回公网 IP | 路由器不支持通配符/单条域名转发，改用 dnsmasq |
| 部分设备生效 | 设备用了硬编码 DNS（如 8.8.8.8），需在 DHCP 里强制下发 |
| 改了但设备不连 | 设备缓存了旧解析，断电重启设备 |

---

## 关于 arm64

`compose.yaml` 里没有写 `platform`，默认按宿主架构构建，arm64 可以直接跑。

如果拉取 `eclipse-mosquitto` 时报架构不匹配，加一行：

```yaml
mqtt:
  platform: linux/arm64
```

---

## 升级

```bash
git pull
docker compose -f deploy/compose.yaml up -d --build
```

**升级会重启 hap，但不会丢配对**——配对信息在 `data/hap.state`，只要这个文件在，
`paired_clients` 就会保持。升级后可用下面的命令确认：

```bash
curl -s -H "Authorization: Bearer $TOKEN" localhost:51827/status | grep paired_clients
```

如果升级后 `paired_clients` 变 0，说明状态文件没保住，检查 `data/` 的挂载与权限。
