#!/usr/bin/env bash
# 生成首次部署所需的本地配置：secrets/ 与 deploy/.env
#
# 做三件事：
#   1. 随机生成管理令牌（bridge 与 hap 共用）
#   2. 写入 secrets/admin_token 与 secrets/master_key（compose 依赖它们）
#   3. 从模板生成 deploy/.env
#
# 幂等：已存在的文件不会覆盖，只提示。要重来请手动删除后重跑。
set -euo pipefail

cd "$(dirname "$0")/.."

SECRETS_DIR="secrets"
ENV_FILE="deploy/.env"
ENV_EXAMPLE="deploy/.env.example"

say() { printf '\033[1;36m%s\033[0m\n' "$*"; }
ok()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn(){ printf '  \033[33m!\033[0m %s\n' "$*"; }

# ---------- 前置检查 ----------
say "[1/4] 检查环境"
command -v docker >/dev/null 2>&1 || { warn "未找到 docker，装好再来"; exit 1; }
ok "docker $(docker --version | awk '{print $3}' | tr -d ',')"
if docker compose version >/dev/null 2>&1; then
  ok "docker compose 可用"
else
  warn "未找到 docker compose，请安装 compose 插件"; exit 1
fi
mkdir -p "$SECRETS_DIR" data

# ---------- 生成令牌 ----------
say "[2/4] 生成管理令牌"
gen() { LC_ALL=C tr -dc 'a-f0-9' </dev/urandom | head -c 32; }

if [ -f "$SECRETS_DIR/admin_token" ]; then
  warn "secrets/admin_token 已存在，保持不变"
  TOKEN=$(cat "$SECRETS_DIR/admin_token")
else
  TOKEN=$(gen)
  printf '%s' "$TOKEN" > "$SECRETS_DIR/admin_token"
  chmod 600 "$SECRETS_DIR/admin_token"
  ok "secrets/admin_token 已生成（32 位十六进制）"
fi

if [ -f "$SECRETS_DIR/master_key" ]; then
  warn "secrets/master_key 已存在，保持不变"
else
  LC_ALL=C tr -dc 'a-f0-9' </dev/urandom | head -c 44 > "$SECRETS_DIR/master_key"
  chmod 600 "$SECRETS_DIR/master_key"
  ok "secrets/master_key 已生成"
fi

# ---------- 生成 .env ----------
say "[3/4] 生成 deploy/.env"
if [ -f "$ENV_FILE" ]; then
  warn "$ENV_FILE 已存在，跳过（如需重置请先删除）"
else
  [ -f "$ENV_EXAMPLE" ] || { warn "缺少 $ENV_EXAMPLE"; exit 1; }
  sed "s|^AIRMX_BRIDGE_TOKEN=.*|AIRMX_BRIDGE_TOKEN=${TOKEN}|" "$ENV_EXAMPLE" > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  ok "$ENV_FILE 已生成"
fi

# ---------- 提示 ----------
say "[4/4] 完成，接下来需要你做两件事"
cat <<'EOT'

  ① 改配对码（强烈建议）
     编辑 deploy/.env 里的 AIRMX_HAP_PINCODE，换成你自己的 8 位数字，
     避开 123-45-678 / 111-11-111 这类。默认值全网公开。

  ② 配置路由器 DNS（否则设备不会连过来）
     把 *.airmx.cn 解析到本机 IP，具体见 docs/03-部署指南.md

  然后启动：
     docker compose -f deploy/compose.yaml up -d

EOT
