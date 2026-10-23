#!/usr/bin/env bash
# 重新生成 CloudDrive2 的 protobuf 消息类。
#
# 什么时候要跑：CD2 升级后 gRPC 字段变了（表现为「调用没报错，但字段读出来是空/0」），
# 或者换了更完整的 CloudDrive.proto。生成结果要提交（运行镜像不带 protoc）。
#
#   bash scripts/gen-cd2-proto.sh
#
# 依赖：pip install grpcio-tools protobuf
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
[ -x "$PY" ] || PY=python3
"$PY" -m grpc_tools.protoc \
  -I app/storage/proto \
  --python_out=app/storage/proto \
  app/storage/proto/CloudDrive.proto
echo "已重新生成 app/storage/proto/CloudDrive_pb2.py"
