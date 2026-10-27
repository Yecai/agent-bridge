import asyncio
import hmac
import json
import socket
from collections import deque
from contextlib import asynccontextmanager
from urllib.parse import urlsplit
from uuid import UUID, uuid5

import uvicorn
from mcp.server.fastmcp import FastMCP, Context
from mcp.types import CallToolResult, TextContent, ImageContent
from mcp.server.transport_security import TransportSecuritySettings

from .protocol import ToolMeta


def build_mcp(runtime):
    mcp = FastMCP("local", stateless_http=True, json_response=True, log_level="ERROR",
                  transport_security=TransportSecuritySettings(allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
                                                               allowed_origins=["http://127.0.0.1:*", "http://localhost:*"]))

    async def call(name, arguments, ctx, op_id=None):
        tool_call_id = str(ctx.request_id)
        identity = op_id or str(uuid5(UUID(runtime.spec.execution_id), "mcp:" + tool_call_id))
        meta = ToolMeta(execution_id=runtime.spec.execution_id, tool_call_id=tool_call_id,
                        op_id=identity, control_revision=runtime.spec.control_revision)
        # FastMCP invokes sync tools on the event loop. Keep process waits,
        # filesystem work and host callbacks off the SSH/lease control loop.
        result = await asyncio.to_thread(runtime.call, name, arguments, meta)
        data = result.get('data', {})
        public = {**result, 'data': {k: v for k, v in data.items() if k != 'image'}}
        content = [TextContent(type='text', text=json.dumps(public, ensure_ascii=False))]
        if data.get('image'):
            content.append(ImageContent(type='image', data=data['image'], mimeType=data['mime_type']))
        return CallToolResult(content=content, structuredContent=public, isError=result['status'] == 'error')

    @mcp.tool()
    async def workspace_context(ctx: Context) -> CallToolResult:
        """获取实际本机工作区、平台、项目规则和已授权资源。"""
        return await call("workspace_context", {}, ctx)

    @mcp.tool()
    async def read_file(path: str, ctx: Context, offset: int = 0, limit: int = 1048576) -> CallToolResult:
        """读取本机文件；version 用于后续编辑的并发校验。"""
        return await call("read_file", {"path": path, "offset": offset, "limit": limit}, ctx)

    @mcp.tool()
    async def list_directory(ctx: Context, path: str = ".", cursor: int = 0, limit: int = 500) -> CallToolResult:
        """分页列出授权本机目录。"""
        return await call("list_directory", {"path": path, "cursor": cursor, "limit": limit}, ctx)

    @mcp.tool()
    async def glob(pattern: str, ctx: Context, path: str = ".", cursor: str | None = None, limit: int = 500) -> CallToolResult:
        """按模式在本机查找文件；需要递归时使用 **/。"""
        return await call("glob", {"pattern": pattern, "path": path, "cursor": cursor, "limit": limit}, ctx)

    @mcp.tool()
    async def grep(pattern: str, ctx: Context, path: str = ".", cursor: str | None = None, limit: int = 500) -> CallToolResult:
        """在授权本机目录搜索文本，返回文件、行号和分页游标。"""
        return await call("grep", {"pattern": pattern, "path": path, "cursor": cursor, "limit": limit}, ctx)

    @mcp.tool()
    async def write_file(path: str, content: str, ctx: Context, expected_version: str | None = None,
                   create: bool = False, op_id: str | None = None) -> CallToolResult:
        """写本机文件。新文件 create=true；覆盖必须传 read_file 得到的 expected_version。"""
        return await call("write_file", {"path": path, "content": content, "expected_version": expected_version, "create": create}, ctx, op_id)

    @mcp.tool()
    async def apply_patch(path: str, expected_version: str, old: str, new: str, ctx: Context, op_id: str | None = None) -> CallToolResult:
        """用唯一精确片段替换本机文件，版本变化时拒绝覆盖。"""
        return await call("apply_patch", {"path": path, "expected_version": expected_version, "old": old, "new": new}, ctx, op_id)

    @mcp.tool()
    async def exec(command: str | list[str], ctx: Context, cwd: str = ".", timeout: int = 1800, op_id: str | None = None) -> CallToolResult:
        """在本机执行Git、构建和测试；验证优先传argv数组。字符串使用Bash pipefail。返回process_id后用process_output取完整输出。"""
        return await call("exec", {"command": command, "cwd": cwd, "timeout": timeout}, ctx, op_id)

    @mcp.tool()
    async def process_output(process_id: str, ctx: Context, cursor: str | int = 0, wait_seconds: int = 1) -> CallToolResult:
        """读取本次执行的命令输出。传回 next_cursor 继续读取，不重复运行命令。"""
        return await call("process_output", {"process_id": process_id, "cursor": cursor, "wait_seconds": wait_seconds}, ctx)

    @mcp.tool()
    async def process_stop(process_id: str, ctx: Context, reason: str = "cancelled", op_id: str | None = None) -> CallToolResult:
        """停止本次命令进程组并确认退出。"""
        return await call("process_stop", {"process_id": process_id, "reason": reason}, ctx, op_id)

    @mcp.tool()
    async def dev_handoff(plan: str, spec: str, ctx: Context, op_id: str | None = None) -> CallToolResult:
        """提交本机规格和计划的交接请求；accepted 后停止进一步写操作。"""
        return await call("dev_handoff", {"plan": plan, "spec": spec}, ctx, op_id)

    @mcp.tool()
    async def dev_finish(summary: str, ctx: Context, op_id: str | None = None) -> CallToolResult:
        """由本机宿主核验真实交付证明后完成任务。"""
        return await call("dev_finish", {"summary": summary}, ctx, op_id)

    for name in (set(READ_NAMES) | {"write_file", "apply_patch", "exec", "process_output", "process_stop", "dev_handoff", "dev_finish"}) - runtime.allowed_tools:
        mcp.remove_tool(name)
    return mcp


def build_server(runtime):
    return AuthGate(build_mcp(runtime).streamable_http_app(), runtime)


READ_NAMES = {"workspace_context", "read_file", "list_directory", "glob", "grep"}


class AuthGate:
    def __init__(self, app, runtime):
        self.app, self.runtime = app, runtime

    async def __call__(self, scope, receive, send):
        started = False
        async def tracked(message):
            nonlocal started
            if message['type'] == 'http.response.start':
                started = True
            await send(message)
        try:
            return await self.handle(scope, receive, tracked)
        except Exception:
            self.runtime.revoke()
            try:
                await asyncio.to_thread(self.runtime.processes.stop_all)
            except Exception:
                pass
            if started:
                raise
            await send({'type': 'http.response.start', 'status': 503, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'{"error":"audit_unavailable"}'})

    async def handle(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        host = headers.get(b"host", b"").decode()
        origin = headers.get(b"origin", b"").decode()
        good_origin = not origin or urlsplit(origin).hostname in {"127.0.0.1", "localhost", "::1"}
        good_host = urlsplit("http://" + host).hostname in {"127.0.0.1", "localhost", "::1"}
        authenticated = hmac.compare_digest(headers.get(b"authorization", b""), ("Bearer " + self.runtime.token).encode())
        if not (good_host and good_origin and authenticated):
            code = 401 if not authenticated else 403
            self.runtime.audit.append("mcp.denied", "error", {"http_status": code})
            await send({"type": "http.response.start", "status": code, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": b'{"error":"access_denied"}'})
            return
        messages, body = deque(), bytearray()
        while True:
            message = await receive()
            messages.append(message)
            body.extend(message.get("body", b""))
            if len(body) > 8 * 1024 * 1024:
                await send({"type": "http.response.start", "status": 413, "headers": []})
                await send({"type": "http.response.body", "body": b""})
                return
            if not message.get("more_body"):
                break
        if scope.get('path') == '/hooks':
            try:
                event = json.loads(body)
                refs = self.runtime.artifacts.put(bytes(body))
                self.runtime.audit.append('claude.hook', 'received',
                                          {'hook': event.get('hook_event_name'), 'tool': event.get('tool_name')},
                                          tool_call_id=event.get('tool_use_id'), artifact_refs=refs,
                                          session_id=event.get('session_id'))
                await asyncio.to_thread(self.runtime.hook, event)
                status, content = 200, b'{}'
            except Exception:
                self.runtime.revoke()
                status, content = 503, b'{"error":"audit_unavailable"}'
            await send({'type': 'http.response.start', 'status': status, 'headers': [(b'content-type', b'application/json')]})
            await send({'type': 'http.response.body', 'body': content})
            return
        if body:
            refs = self.runtime.artifacts.put(bytes(body))
            self.runtime.audit.append("mcp.request", "received", {}, artifact_refs=refs)
        try:
            request_method = json.loads(body).get('method')
        except (ValueError, AttributeError):
            request_method = None
        response = bytearray()
        response_status = 200

        async def replay():
            return messages.popleft() if messages else await receive()

        async def captured(message):
            nonlocal response_status
            if message['type'] == 'http.response.start':
                response_status = message['status']
            if message["type"] == "http.response.body":
                response.extend(message.get("body", b""))
                if not message.get("more_body"):
                    refs = self.runtime.artifacts.put(bytes(response))
                    try:
                        payload = json.loads(response)
                        failed = response_status >= 400 or 'error' in payload or payload.get('result', {}).get('isError', False)
                    except ValueError:
                        failed = response_status >= 400
                    self.runtime.audit.append("mcp.response", "error" if failed else "ok", {}, artifact_refs=refs)
                    if request_method == 'tools/list' and not failed:
                        self.runtime.mcp_ready = True
            await send(message)

        await self.app(scope, replay, captured)


@asynccontextmanager
async def serve(runtime):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    server = uvicorn.Server(uvicorn.Config(build_server(runtime), log_level="error", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _ in range(200):
            if server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(0.01)
        if not server.started:
            raise RuntimeError("MCP 服务启动超时")
        yield f"http://127.0.0.1:{listener.getsockname()[1]}/mcp"
    finally:
        server.should_exit = True
        await task
        listener.close()
