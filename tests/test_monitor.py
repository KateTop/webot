import concurrent.futures
import pytest
from src.monitor import Monitor, monitor, current_trace, observed_call, monitor_task

def test_bounded_clear_and_copy():
    m=Monitor(2)
    a=m.begin({"content":"synthetic"}); m.begin({}); c=m.begin({})
    assert m.snapshot(a) is None
    for _ in range(100): m.event("step", "x"*20000, c)
    row=m.snapshot(c)
    assert len(row["events"])==80
    assert "显示截断" in row["events"][-1]["detail"]
    row["events"].clear()
    assert len(m.snapshot(c)["events"])==80
    m.clear(); assert m.snapshot()==[]

def test_thread_context_isolation():
    m=Monitor()
    def run(n):
        trace=m.begin({"content":str(n)})
        token=current_trace.set(trace)
        try: m.event("thread", n)
        finally: current_trace.reset(token)
        return trace
    with concurrent.futures.ThreadPoolExecutor(4) as pool: ids=list(pool.map(run,range(4)))
    assert len(set(ids))==4
    for n,t in enumerate(ids): assert m.snapshot(t)["events"][-1]["detail"]==str(n)

def test_error_does_not_expose_credentials_and_return_unchanged():
    trace=monitor.begin({})
    token=current_trace.set(trace)
    try:
        class P: model="synthetic-model"
        assert observed_call(P(),"reply","synthetic",[],lambda:"body")=="body"
        def fail(): raise RuntimeError("secret-test-key")
        with pytest.raises(RuntimeError): observed_call(P(),"reply","synthetic",[],fail)
        assert "secret-test-key" not in str(monitor.snapshot(trace))
    finally: current_trace.reset(token);monitor.clear()

def test_background_task_and_cleared_inflight():
    @monitor_task("synthetic")
    def task(): return "NO_UPDATE"
    assert task()=="NO_UPDATE"
    assert current_trace.get() is None
    assert monitor.snapshot()[0]["status"]=="AI任务完成"
    trace=monitor.begin({});monitor.clear();monitor.event("late",{},trace)
    assert monitor.snapshot()==[]

def test_local_api_list_detail_clear_and_origin():
    import json
    from tests.test_web_api import _build_handler
    def result(sock): return json.loads(sock.get_response_text().split("\r\n\r\n",1)[1])
    monitor.clear()
    trace=monitor.begin({"content":"synthetic"})
    _,sock=_build_handler("/api/monitor")
    assert result(sock)["records"][0]["id"]==trace
    _,sock=_build_handler("/api/monitor?id="+trace)
    assert result(sock)["records"]["events"][0]["stage"]=="收到消息"
    _,sock=_build_handler("/api/monitor",headers={"Origin":"https://example.com"})
    assert not result(sock)["ok"]
    _,sock=_build_handler("/api/monitor/clear")
    assert not result(sock)["ok"]
    _,sock=_build_handler("/api/monitor/clear",method="POST")
    assert result(sock)["ok"] and monitor.snapshot()==[]

def test_router_reply_and_delivery_keep_identity():
    from src.router import MessageRouter
    from unittest.mock import Mock
    r=MessageRouter.__new__(MessageRouter)
    r._handle_monitored=Mock(return_value="synthetic reply")
    msg={"chat_id":"synthetic-group","content":"synthetic"}
    assert r.handle(msg)=="synthetic reply"
    assert current_trace.get() is None
    r._proactive=Mock()
    r.record_delivery(msg,False,"")
    row=monitor.snapshot(msg["monitor_trace"])
    assert row["status"]=="发送失败或未确认"
    assert row["events"][-1]["stage"]=="微信库确认"
    monitor.clear()

def test_sdk_capture_only_body_and_preserve_response():
    from src.summarize.base import AbstractSummarizer
    trace=monitor.begin({})
    token=current_trace.set(trace)
    try:
        obj=object()
        response=AbstractSummarizer._monitored_create(None,lambda **kw:obj,
            model="synthetic",messages=[{"role":"user","content":"synthetic"}],extra_headers={"Authorization":"secret-test-key"})
        assert response is obj
        row=monitor.snapshot(trace)
        assert "secret-test-key" not in str(row)
        assert row["events"][-2]["stage"]=="AI实际输入"
    finally:current_trace.reset(token);monitor.clear()
