"""Loopback MCP reader; bounded pages, strict realtime, no native WCDB initialization."""
import json
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse
from src.config import PROJECT_ROOT

CONNECTION_FILE = PROJECT_ROOT / "data" / "mcp_connection.json"
_connection_lock = threading.RLock()


def load_connection():
    try:
        data = json.loads(CONNECTION_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    if not isinstance(data, dict):
        raise ValueError("MCP连接配置无效")
    return {"endpoint":data.get("endpoint", "http://127.0.0.1:13818/mcp"),
            "token":data.get("token", ""), "account":data.get("account", "")}


def save_connection(data):
    with _connection_lock:
        return _save_connection(data)


def _save_connection(data):
    if not isinstance(data, dict):
        raise ValueError("MCP配置必须是对象")
    current = load_connection()
    for key in ("endpoint", "token", "account"):
        if key in data:
            if not isinstance(data[key], str) or len(data[key]) > 2048:
                raise ValueError("MCP连接字段无效")
            if key == "token" and data[key] == "********":
                continue
            current[key] = data[key].strip()
    validate_endpoint(current["endpoint"])
    CONNECTION_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONNECTION_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
    tmp.replace(CONNECTION_FILE)
    return {**current, "token":"********" if current["token"] else ""}


def validate_endpoint(endpoint):
    url = urlparse(endpoint)
    if url.scheme != "http" or url.hostname not in ("127.0.0.1", "localhost", "::1") or url.username or url.password or url.fragment:
        raise ValueError("MCP地址必须是本机HTTP地址")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError("MCP接口重定向被拒绝")


class McpWeChatClient:
    def __init__(self, connection=None):
        self.connection = connection if connection is not None else load_connection()
        validate_endpoint(self.connection["endpoint"])
        if not self.connection.get("token"):
            raise ValueError("请在微信后端配置中填写MCP访问令牌")
        self._session = None
        self._protocol = "2025-06-18"
        self._request_id = 0
        self._lock = threading.RLock()
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        self._config = {}
        self._nicknames = {}
        self._members = {}
        self._account = self.connection.get("account", "")
        self.tools = set()

    def _rpc(self, method, params=None, notification=False):
        with self._lock:
            self._request_id += 1
            identifier = self._request_id
            payload = {"jsonrpc":"2.0", "method":method}
            if not notification:
                payload["id"] = identifier
            if params is not None:
                payload["params"] = params
            headers = {"Content-Type":"application/json", "Accept":"application/json, text/event-stream",
                       "Authorization":"Bearer " + self.connection["token"]}
            if self._session:
                headers["Mcp-Session-Id"] = self._session
            if method != "initialize":
                headers["MCP-Protocol-Version"] = self._protocol
            request = urllib.request.Request(self.connection["endpoint"], data=json.dumps(payload).encode(), headers=headers)
            try:
                with self._opener.open(request, timeout=10) as response:
                    self._session = response.headers.get("Mcp-Session-Id") or self._session
                    if notification or response.status == 202:
                        return {}
                    if "text/event-stream" in response.headers.get("Content-Type", ""):
                        deadline = time.monotonic() + 15
                        consumed = 0
                        for _ in range(128):
                            line = response.readline(131072)
                            consumed += len(line)
                            if consumed > 2097152 or time.monotonic() > deadline or not line:
                                break
                            line = line.decode().strip()
                            if line.startswith("data:"):
                                result = json.loads(line[5:].strip())
                                if result.get("id") == identifier:
                                    break
                        else:
                            raise RuntimeError("MCP事件读取超出预算")
                        if not isinstance(locals().get("result"), dict) or result.get("id") != identifier:
                            raise RuntimeError("MCP未返回对应请求")
                    else:
                        raw = response.read(2097153)
                        if len(raw) > 2097152:
                            raise RuntimeError("MCP结果过大，请缩小分页")
                        result = json.loads(raw)
            except urllib.error.HTTPError as exc:
                raise RuntimeError(f"MCP接口HTTP {exc.code}，请检查服务和访问令牌") from None
            except (urllib.error.URLError, TimeoutError, OSError):
                raise RuntimeError("无法连接本机WeChatDataAnalysis MCP，请保持服务运行") from None
            if result.get("error") or result.get("id") != identifier:
                raise RuntimeError("MCP协议请求失败：" + method)
            return result.get("result", {})

    def _tool(self, name, arguments=None):
        args = dict(arguments or {})
        if self._account and name != "wechat.core.get_status":
            args["account"] = self._account
        result = self._rpc("tools/call", {"name":name, "arguments":args})
        if result.get("isError"):
            raise RuntimeError("MCP工具调用失败：" + name)
        data = result.get("structuredContent")
        if data is None:
            for block in result.get("content", []):
                if block.get("type") == "text":
                    try:
                        data = json.loads(block["text"])
                        break
                    except ValueError:
                        continue
        if not isinstance(data, dict) or data.get("status") in ("error", "failed", "failure"):
            raise RuntimeError("MCP工具返回无效结果：" + name)
        return data

    @staticmethod
    def _require_realtime(data):
        if data.get("source") != "realtime" or data.get("sourceFallback"):
            raise RuntimeError("MCP未使用实时数据，暂停读取以避免旧快照触发回复")

    def init(self):
        initialized = self._rpc("initialize", {"protocolVersion":self._protocol, "capabilities":{},
            "clientInfo":{"name":"WeBot", "version":"mcp-reader-1"}})
        self._protocol = initialized["protocolVersion"]
        self._rpc("notifications/initialized", notification=True)
        cursor = None
        for _ in range(100):
            page = self._rpc("tools/list", {"cursor":cursor} if cursor else {})
            self.tools.update(tool["name"] for tool in page.get("tools", []))
            cursor = page.get("nextCursor")
            if not cursor:
                break
        if cursor:
            raise RuntimeError("MCP工具分页超出预算")
        required = {"wechat.core.get_account_info", "wechat.chat.list_sessions", "wechat.chat.get_messages"}
        if not required <= self.tools:
            raise RuntimeError("MCP缺少必要的微信读取工具")

    def open(self):
        info = self._tool("wechat.core.get_account_info")
        realtime = info.get("realtime", {})
        if not realtime.get("connected"):
            raise RuntimeError("WeChatDataAnalysis实时读取未连接，请先在该软件连接微信")
        self._account = info.get("account") or self._account
        own = realtime.get("native_wxid", "")
        if not own and "wechat.moments.get_self_info" in self.tools:
            own = self._tool("wechat.moments.get_self_info").get("wxid", "")
        if not isinstance(own, str) or not own:
            raise RuntimeError("MCP未提供本账号稳定ID，不能安全识别引用或确认发送")
        self._config = {"myWxid":own}
        # Existing member aliases remain useful; no claim that this is a full live roster.
        path = PROJECT_ROOT / "data" / "group_members.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._members = {k:dict(v) for k,v in data.items() if isinstance(v, dict)}
                    for values in self._members.values():
                        self._nicknames.update(values)
            except (ValueError, OSError):
                pass

    def get_sessions(self, limit=10000):
        rows = []
        offset = 0
        while offset < limit:
            size = min(50, limit-offset)
            page = self._tool("wechat.chat.list_sessions", {"source":"realtime", "limit":size, "offset":offset,
                "preview":"none", "include_hidden":False, "include_official":False})
            self._require_realtime(page)
            batch = page.get("sessions", [])
            for row in batch:
                username = row.get("username", "")
                name = row.get("name") or username
                self._nicknames[username] = name
                rows.append({**row, "displayName":name})
            if not page.get("hasMore") or not batch:
                break
            offset += len(batch)
        return rows

    def _normalize(self, row, talker):
        own = self._config["myWxid"]
        sender = own if row.get("isSent") else row.get("senderUsername", "")
        display = row.get("senderDisplayName") or sender
        if sender and display:
            self._nicknames[sender] = display
            self._members.setdefault(talker, {})[sender] = display
        server = row.get("serverIdStr")
        if not server:
            candidate = row.get("serverId", 0)
            # Never use a float-rounded large server ID.
            server = str(candidate) if isinstance(candidate, (int, str)) else "0"
        kind = int(row.get("type", 1) or 1) & 0xffffffff
        body = row.get("content", "")
        if not isinstance(body, str):
            body = ""
        render_type = str(row.get("renderType", ""))
        if not body:
            body = row.get("voiceTranscript") or row.get("title") or ("["+render_type+"]" if render_type else "[非文本消息]")
        at_users = row.get("atUsernames") or []
        if isinstance(at_users, str):
            at_users = [value for value in re.split(r"[,;\s]+", at_users) if value]
        if not isinstance(at_users, list):
            at_users = []
        at_users = [value for value in at_users if isinstance(value, str)]
        return {**row, "sender_username":sender, "message_content":body,
            "server_id":str(server), "local_id":talker+":"+str(row.get("localId") or row.get("id") or row.get("sortSeq")),
            "create_time":int(row.get("createTime", 0)), "localType":kind, "mcp_structured":True,
            "mcp_at_users":at_users, "mcp_quote_username":row.get("quoteUsername") or "",
            "mcp_quote_content":row.get("quoteContent") or ""}

    def get_messages(self, talker, limit=200, offset=0):
        rows = []
        while len(rows) < limit:
            size = min(50, limit-len(rows))
            page = self._tool("wechat.chat.get_messages", {"source":"realtime", "username":talker, "limit":size,
                "offset":offset+len(rows), "order":"desc"})
            self._require_realtime(page)
            batch = page.get("messages", [])
            rows.extend(self._normalize(row, talker) for row in batch)
            if not page.get("hasMore") or not batch:
                break
        return rows

    def resolve_nickname(self, wxid):
        return self._nicknames.get(wxid, wxid)

    def get_display_names(self, usernames):
        return {wxid:self.resolve_nickname(wxid) for wxid in usernames}

    def get_group_members(self, chat_id):
        return [{"username":wxid} for wxid in self._members.get(chat_id, {})]

    def close(self):
        self._session = None
