"""最小 WebSocket 客户端（RFC 6455，纯标准库实现）。

为什么自己写：
    阿里云百炼的「实时语音合成 Sambert」只提供 WebSocket 接口（wss），
    而 Python 标准库没有 WebSocket 客户端；官方 dashscope SDK 与
    websockets 库都属于第三方依赖。本项目的约定是「零第三方依赖」
    （见 README 与 docs/handoff.md），因此这里用手头的 socket / ssl /
    base64 / hashlib 实现一个够用的客户端：只有握手、收发帧、关闭，
    不做子协议协商、不做压缩扩展。

能力边界（刻意保持最小）：
    * 客户端发出的帧一律带 mask（RFC 强制要求）。
    * 接收时处理 7 / 16 / 64 位长度、分片（continuation）合并、
      ping 自动回 pong、close 帧回报。
    * wss 走 ssl，支持 no_verify（本机缺 CA 根证书时的绕过开关，默认关闭）。

仅覆盖本项目用到的部分，未实现的特性在收到对应帧时会明确报错而不是静默出错。
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
from typing import Optional
from urllib.parse import urlparse

# RFC 6455 规定的握手魔串
_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WSError(RuntimeError):
    """WebSocket 连接 / 协议层面的可预期失败。"""


def _recv_exactly(sock: socket.socket, n: int) -> bytes:
    """读满 n 字节，读不到视为连接断开。"""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise WSError("连接已断开（对端关闭）")
        buf.extend(chunk)
    return bytes(buf)


class MinimalWS:
    """一个够用的 WebSocket 客户端连接。

    用法：
        with MinimalWS("wss://host/path", {"Authorization": "Bearer x"}) as ws:
            ws.send_text('{"hello": 1}')
            opcode, payload = ws.recv()
    """

    def __init__(
        self,
        url: str,
        headers: Optional[dict[str, str]] = None,
        timeout: float = 30.0,
        no_verify: bool = False,
    ) -> None:
        self.url = url
        self.headers = headers or {}
        self.timeout = timeout
        self.no_verify = no_verify
        self.sock: Optional[socket.socket] = None
        self._closed = False

    # ── 握手 ────────────────────────────────────────────────
    def connect(self) -> "MinimalWS":
        u = urlparse(self.url)
        if u.scheme not in ("ws", "wss"):
            raise WSError(f"不支持的 URL 协议：{u.scheme}（应为 ws 或 wss）")
        host = u.hostname or ""
        port = u.port or (443 if u.scheme == "wss" else 80)
        path = u.path or "/"
        if u.query:
            path = f"{path}?{u.query}"

        key = base64.b64encode(os.urandom(16)).decode()
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        for k, v in self.headers.items():
            lines.append(f"{k}: {v}")
        raw = ("\r\n".join(lines) + "\r\n\r\n").encode()

        try:
            sock = socket.create_connection((host, port), timeout=self.timeout)
            if u.scheme == "wss":
                ctx = (
                    ssl._create_unverified_context()
                    if self.no_verify
                    else ssl.create_default_context()
                )
                # server_hostname 触发 SNI，阿里云网关要求开启
                sock = ctx.wrap_socket(sock, server_hostname=host)
            sock.settimeout(self.timeout)
            sock.sendall(raw)
        except ssl.SSLCertVerificationError as e:  # 证书链校验失败
            raise WSError(
                f"TLS 证书校验失败：{e}；"
                f"可设置 no_verify=true 临时绕过（仅测试环境），"
                f"或配置 SSL_CERT_FILE 指向 CA 根证书"
            ) from e
        except OSError as e:
            raise WSError(f"连接失败：{e}") from e

        # 解析握手响应
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                raise WSError("握手失败：连接被关闭")
            resp += chunk
            if len(resp) > 65536:
                raise WSError("握手响应过长，疑似非 WebSocket 服务")
        head = resp.split(b"\r\n\r\n", 1)[0].decode("utf-8", "replace")
        status = head.split("\r\n", 1)[0]
        if "101" not in status:
            raise WSError(f"握手失败：{status}（请检查 API Key 与端点）")
        accept = ""
        for line in head.split("\r\n")[1:]:
            if ":" in line and line.split(":", 1)[0].strip().lower() == "sec-websocket-accept":
                accept = line.split(":", 1)[1].strip()
        expect = base64.b64encode(hashlib.sha1(key.encode() + _GUID).digest()).decode()
        if accept != expect:
            raise WSError("握手失败：Sec-WebSocket-Accept 校验不通过")
        self.sock = sock
        return self

    # ── 发送 ────────────────────────────────────────────────
    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self.sock is None:
            raise WSError("连接未建立")
        n = len(payload)
        header = bytearray([0x80 | opcode])  # FIN=1；客户端必须 mask
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", n))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", n))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def send_bytes(self, data: bytes) -> None:
        self._send_frame(OP_BINARY, data)

    # ── 接收 ────────────────────────────────────────────────
    def _recv_frame(self) -> tuple[bool, int, bytes]:
        """读一帧（不合并分片），返回 (fin, opcode, payload)。

        ping 在本方法内自动回 pong；服务端分片发送时用 fin 标记最后一帧。
        """
        if self.sock is None:
            raise WSError("连接未建立")
        while True:
            b0, b1 = _recv_exactly(self.sock, 2)
            fin = bool(b0 & 0x80)
            opcode = b0 & 0x0F
            masked = b1 & 0x80
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack("!H", _recv_exactly(self.sock, 2))[0]
            elif length == 127:
                length = struct.unpack("!Q", _recv_exactly(self.sock, 8))[0]
            mask = _recv_exactly(self.sock, 4) if masked else b""
            payload = _recv_exactly(self.sock, length) if length else b""
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == OP_PING:
                self._send_frame(OP_PONG, payload)  # 自动回 pong，保活
                continue
            if opcode == OP_PONG:
                continue
            return fin, opcode, payload

    def recv(self) -> tuple[int, bytes]:
        """接收一条完整消息（自动合并分片）。返回 (opcode, payload)。

        Sambert 的音频流每个分片是一个独立 binary 消息，因此调用方
        通常「每收一个 binary 就追加」，而不是等一个大消息。
        """
        fin, opcode, payload = self._recv_frame()
        if opcode == OP_CLOSE:
            return OP_CLOSE, payload
        if opcode == OP_CONT:
            raise WSError("收到孤立的续帧（continuation），协议异常")
        if opcode not in (OP_TEXT, OP_BINARY):
            raise WSError(f"暂不支持的 WebSocket 帧类型：0x{opcode:x}")
        if fin:
            return opcode, payload
        # 分片消息：持续读续帧直到 FIN
        buf = bytearray(payload)
        while not fin:
            fin, op, chunk = self._recv_frame()
            if op != OP_CONT:
                raise WSError(f"分片消息中收到非续帧：0x{op:x}")
            buf.extend(chunk)
        return opcode, bytes(buf)

    # ── 关闭 ────────────────────────────────────────────────
    def close(self) -> None:
        if self._closed or self.sock is None:
            return
        try:
            self._send_frame(OP_CLOSE, struct.pack("!H", 1000))
        except Exception:  # noqa: BLE001  关闭阶段的异常无意义
            pass
        try:
            self.sock.close()
        except Exception:  # noqa: BLE001
            pass
        self._closed = True
        self.sock = None

    def __enter__(self) -> "MinimalWS":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()
