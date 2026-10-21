#!/usr/bin/env bash
# 把实测有效的 aria2 参数写进 aria2-config/aria2.conf（幂等，可反复执行）。
#
# 为什么单独一个脚本：p3terx/aria2-pro 镜像只在 /config 为空时生成默认配置，
# 之后一直用挂载目录里的 aria2.conf。所以调优必须落到这个文件上，
# 而且要让「新机器首次部署」和「老机器升级」都得到同一套参数。
#
# 实测依据（Ubuntu 3.3GB 官方种子，2 任务并发，各 75s，取后 1/3 稳态）：
#   基线（bt-force-encryption=true）  40.96 MB/s  conns 49  seeders 22
#   关闭强制加密                      61.96 / 53.69 MB/s（两次）  conns 88/69  seeders 42/26
#   bt-max-peers=256                  49.71 MB/s  conns 40  seeders 16  （无效）
#   关加密 + max-peers=256 + 速度阈值  60.30 MB/s  conns 66  seeders 28  （无额外收益）
#   关加密 + max-concurrent-downloads=10  57.96 MB/s（无额外收益）
# 结论：唯一有正收益的是关掉强制加密（peer 数翻倍）。其余参数保持默认，
#       BT 场景下聚合速度由「种子本身的 peer 供给」决定，不是带宽也不是并发上限。
#
# 带宽实测（cachefly 100MB 文件，1/2/4/8 路并发）：
#   1 路 73-79 MB/s、2 路 81-85 MB/s、4 路 88-89 MB/s、8 路 87 MB/s → 链路约 88MB/s 封顶
set -euo pipefail

CONF="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/aria2-config/aria2.conf}"

if [ ! -f "$CONF" ]; then
    echo "找不到 $CONF（先让 aria2 容器启动一次以生成默认配置）" >&2
    exit 1
fi

# set_key 文件 键 值 —— 有则改、无则追加
set_key() {
    local file="$1" key="$2" value="$3"
    if grep -qE "^[[:space:]]*#?[[:space:]]*${key}=" "$file"; then
        # 连同行首注释一起换成生效配置，避免出现两行同名键
        sed -i -E "s|^[[:space:]]*#?[[:space:]]*${key}=.*|${key}=${value}|" "$file"
    else
        printf '%s=%s\n' "$key" "$value" >>"$file"
    fi
}

# 关掉强制加密：拒绝明文握手会过滤掉大量只支持明文的 peer。实测连接数 49→88。
set_key "$CONF" bt-force-encryption false
# 这两项跟随上面的开关一起显式关掉，避免镜像默认值把它们打开
set_key "$CONF" bt-require-crypto false
set_key "$CONF" bt-min-crypto-level plain

echo "已写入 $CONF："
grep -E '^(bt-force-encryption|bt-require-crypto|bt-min-crypto-level)=' "$CONF"
