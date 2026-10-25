"""CloudDrive2 的 gRPC 通道与最小方法桩。

为什么不直接用官方 `_pb2_grpc.py`：那个文件 319KB，全是每个方法的样板与
grpc 版本断言，而本项目只用得到十来个调用。这里用 channel 的
`unary_unary` / `unary_stream` / `stream_unary` 直接按 `/服务名/方法名` 拼。

鉴权：`GetToken` 换 JWT，之后每个调用带 metadata `authorization: Bearer <token>`。
token 有 expiration，这里提前 5 分钟换新的；遇 UNAUTHENTICATED 清空重登一次
（只一次，避免密码错时死循环）。

`import grpc` 放在函数里延迟导入：镜像可能是 WITH_CD2=0 构建的，
其它后端不该因为缺 grpcio 就整个起不来。
"""
from __future__ import annotations

import time
from urllib.parse import urlsplit

from .base import AuthError, StorageError
from .proto import CloudDrive_pb2 as pb

SERVICE = "clouddrive.CloudDriveFileSrv"
REFRESH_BEFORE = 300.0          # token 剩余不足 5 分钟就主动换

# gRPC 单条消息的客户端上限。
#
# 注意：CD2 **服务端**自己把单条消息限制在 4MB，抬客户端上限解决不了
# "Received message larger than max" —— 真正要管的是 chunk 大小
# （见 clouddrive2.CloudDrive2.MAX_CHUNK_MB）。这里抬高只是为了让客户端
# 不会先于服务端拒绝大响应（比如目录列表特别长时），不是上传能成的关键。
MAX_MESSAGE_BYTES = 32 * 1024 * 1024

UNARY, STREAM_OUT, STREAM_IN = "unary", "stream_out", "stream_in"


def _grpc():
    try:
        import grpc                                  # noqa: PLC0415（延迟导入是刻意的）
    except ImportError as exc:
        raise AuthError(
            "clouddrive2：运行环境没有 grpcio（镜像若用 WITH_CD2=0 构建，请改后端或重建）"
        ) from exc
    return grpc


class Cd2Channel:
    """一条连接 + 一个 token。同步实现：网页在后台线程用，bot 用 to_thread 包。"""

    def __init__(self, url: str, user: str = "", password: str = "", *,
                 tls: bool = False, ca_file: str = "", timeout: float = 60.0,
                 task_timeout: float = 7200.0):
        if not url:
            raise AuthError("clouddrive2：没配 CD2_URL")
        parts = urlsplit(url if "//" in url else f"//{url}")
        host = parts.hostname or "127.0.0.1"
        port = parts.port or 19798
        self.target = f"{host}:{port}"
        self.tls = bool(tls or (parts.scheme or "").lower() == "https")
        self.ca_file = ca_file or ""
        self.user = user or ""
        self.password = password or ""
        self.timeout = float(timeout or 60)
        self.task_timeout = float(task_timeout or 7200)
        self._token = ""
        self._expire_at = 0.0
        grpc = _grpc()
        options = [("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
                   ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES)]
        self._options = tuple(options)
        if self.tls:
            creds = (grpc.ssl_channel_credentials(open(self.ca_file, "rb").read())
                     if self.ca_file else grpc.ssl_channel_credentials())
            self._channel = grpc.secure_channel(self.target, creds, options=options)
        else:
            self._channel = grpc.insecure_channel(self.target, options=options)

    def close(self) -> None:
        if getattr(self, "_channel", None) is not None:
            try:
                self._channel.close()
            finally:
                self._channel = None

    # ── 调用
    def _stub(self, method: str, response_cls, mode: str):
        path = f"/{SERVICE}/{method}"
        kw = dict(request_serializer=lambda m: m.SerializeToString(),
                  response_deserializer=response_cls.FromString)
        if mode == STREAM_OUT:
            return self._channel.unary_stream(path, **kw)
        if mode == STREAM_IN:
            return self._channel.stream_unary(path, **kw)
        return self._channel.unary_unary(path, **kw)

    def _metadata(self) -> tuple:
        return (("authorization", f"Bearer {self._token}"),) if self._token else ()

    def call(self, method: str, request, response_cls, *, timeout: float | None = None,
             mode: str = UNARY, auth: bool = True):
        """发一个调用。

        request：mode=STREAM_IN 时必须是「返回迭代器的零参函数」——重试要重新生成
        一份请求流，一次性生成器用完就没了。
        """
        grpc = _grpc()
        if auth and (not self._token or self._expiring()):
            self.login()
        for attempt in (0, 1):
            stub = self._stub(method, response_cls, mode)
            payload = request() if mode == STREAM_IN else request
            try:
                result = stub(payload, timeout=timeout or self.timeout,
                              metadata=self._metadata() if auth else ())
                # 服务端流：在重试循环内就把迭代走完。惰性返回的话，
                # 真正的 RPC 错误会在调用方的 for 循环里冒出来 —— 那时既翻译不了
                # 也重登不了（列目录"偶发 UNAUTHENTICATED"就是这么来的）。
                return list(result) if mode == STREAM_OUT else result
            except grpc.RpcError as exc:
                if (auth and attempt == 0 and exc.code() == grpc.StatusCode.UNAUTHENTICATED):
                    self._token = ""             # token 过期：换一个再试一次
                    self.login()
                    continue
                raise self._translate(exc) from exc
        raise StorageError("clouddrive2：鉴权反复失败")

    def _translate(self, exc) -> Exception:
        grpc = _grpc()
        code = getattr(exc, "code", lambda: None)()
        try:
            detail = exc.details() or ""
        except Exception:
            detail = ""
        text = str(detail or code)
        if code in (grpc.StatusCode.UNAUTHENTICATED, grpc.StatusCode.PERMISSION_DENIED,
                    grpc.StatusCode.INVALID_ARGUMENT, grpc.StatusCode.FAILED_PRECONDITION):
            return AuthError(f"clouddrive2 鉴权/参数错误：{text}")
        return StorageError(f"clouddrive2 调用失败：{text}")

    def _expiring(self) -> bool:
        return bool(self._expire_at) and time.time() > self._expire_at - REFRESH_BEFORE

    def login(self) -> str:
        resp = self.call("GetToken",
                         pb.GetTokenRequest(userName=self.user, password=self.password),
                         pb.JWTToken, auth=False)
        if not getattr(resp, "success", False) or not resp.token:
            raise AuthError(f"clouddrive2：登录失败 {resp.errorMessage or '（账号或密码错误）'}")
        self._token = resp.token
        expire = getattr(resp, "expiration", None)
        self._expire_at = float(expire.seconds) if expire is not None and expire.seconds else 0.0
        return self._token
