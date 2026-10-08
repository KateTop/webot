"""Bounded process-local diagnostic records, never persisted or logged."""
import copy
import threading
import time
import uuid
from collections import OrderedDict
from contextvars import ContextVar

current_trace = ContextVar("bot_monitor_trace", default=None)

class Monitor:
    def __init__(self, capacity=100):
        self.capacity = capacity
        self.lock = threading.RLock()
        self.records = OrderedDict()

    def begin(self, msg):
        trace = uuid.uuid4().hex
        with self.lock:
            self.records[trace] = {"id": trace, "time": time.time(), "group": str(msg.get("chat_id", ""))[:200],
                "sender": str(msg.get("sender_name", ""))[:200], "status": "处理中", "events": []}
            while len(self.records) > self.capacity:
                self.records.popitem(last=False)
        self.event("收到消息", {k: msg.get(k) for k in ("content", "quoted_content", "is_at_mentioned", "quotes_bot", "is_self", "timestamp")}, trace)
        return trace

    def event(self, stage, data=None, trace=None, status=None):
        trace = trace or current_trace.get()
        if not trace:
            return
        # Each record <= 80 events, each event <= 12,000 displayed chars.
        import json
        body = json.dumps(data, ensure_ascii=False, default=lambda _: "[不可显示对象]")
        truncated = len(body) > 12000
        value = body[:12000] + ("\n[显示截断；实际调用未截断]" if truncated else "")
        with self.lock:
            row = self.records.get(trace)
            if row is None:
                return
            if len(row["events"]) < 80:
                row["events"].append({"stage": stage, "elapsed": round(time.time()-row["time"], 3), "detail": value})
            if status:
                row["status"] = status

    def snapshot(self, trace=None):
        with self.lock:
            if trace:
                return copy.deepcopy(self.records.get(trace))
            return [{k:v for k,v in row.items() if k != "events"} for row in reversed(list(self.records.values()))]

    def clear(self):
        with self.lock:
            self.records.clear()

monitor = Monitor()

def observed_call(provider, kind, prompt, messages, call):
    monitor.event("AI调用开始", {"task":kind, "provider":type(provider).__name__, "model":getattr(provider, "model", "")})
    start = time.monotonic()
    try:
        result = call()
        monitor.event("AI返回", {"task":kind, "seconds":round(time.monotonic()-start,3), "output":result})
        return result
    except Exception as exc:
        # Do not expose SDK exception text: it can contain headers/credentials.
        monitor.event("AI调用失败", {"task":kind, "error_type":type(exc).__name__, "seconds":round(time.monotonic()-start,3)})
        raise


def monitor_task(kind):
    """Give background AI tasks their own context without sharing mutable state."""
    from functools import wraps
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if current_trace.get():
                return fn(*args, **kwargs)
            trace = monitor.begin({"sender_name":kind})
            token = current_trace.set(trace)
            try:
                value = fn(*args, **kwargs)
                monitor.event("任务完成", {"task":kind}, status="AI任务完成")
                return value
            except Exception as exc:
                monitor.event("任务失败", {"error_type":type(exc).__name__}, status="处理失败")
                raise
            finally:
                current_trace.reset(token)
        return wrapped
    return decorate
