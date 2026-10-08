import { useEffect, useState } from 'react'
const API = 'http://127.0.0.1:7327/api/monitor'
export default function MonitorPanel() {
  const [rows,setRows] = useState([]), [selected,setSelected] = useState(''), [detail,setDetail] = useState(null)
  const [paused,setPaused] = useState(false), [group,setGroup] = useState(''), [status,setStatus] = useState(''), [error,setError] = useState('')
  useEffect(() => {
    let stopped=false
    async function refresh() {
      try {
        const list=await (await fetch(API)).json()
        if (!list.ok) throw new Error(list.error)
        if (!stopped) {setRows(list.records);setError('')}
        if (selected) {
          const result=await (await fetch(`${API}?id=${encodeURIComponent(selected)}`)).json()
          if (!stopped) setDetail(result.records)
        }
      } catch {if (!stopped) setError('监视数据读取失败，请检查程序是否运行')}
    }
    refresh()
    const timer=paused ? null : setInterval(refresh,2000)
    return () => {stopped=true;clearInterval(timer)}
  },[selected,paused])
  async function clear() {
    try {const result=await (await fetch(`${API}/clear`,{method:'POST'})).json(); if(!result.ok) throw new Error();setRows([]);setSelected('');setDetail(null)}
    catch {setError('清空失败')}
  }
  const visible=rows.filter(r=>(!group||r.group===group)&&(!status||r.status===status))
  return <div className="space-y-4 p-6">
    <h2 className="text-xl font-bold">Bot 监视</h2>
    <p className="text-sm text-text-muted">查看读取内容、规则判断、AI 输入与回复、重试轮换和发送确认。判断依据是可检查的运行信息，不是模型隐藏思维。仅保留本次运行最近 100 条，不写入文件；每条最多 80 个阶段，每阶段最多显示 12000 字符。</p>
    <div className="flex flex-wrap gap-3">
      <button onClick={()=>setPaused(!paused)}>{paused?'恢复刷新':'暂停刷新'}</button>
      <button onClick={clear}>清空监视记录</button>
      <select className="bg-transparent border rounded p-2" value={group} onChange={e=>setGroup(e.target.value)}><option value="">全部群</option>{[...new Set(rows.map(r=>r.group))].map(g=><option key={g} value={g}>{g||'后台任务'}</option>)}</select>
      <select className="bg-transparent border rounded p-2" value={status} onChange={e=>setStatus(e.target.value)}><option value="">全部状态</option>{[...new Set(rows.map(r=>r.status))].map(s=><option key={s}>{s}</option>)}</select>
    </div>
    {error&&<p role="alert">{error}</p>}
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
      <div className="space-y-2 max-h-[70vh] overflow-auto">{!visible.length&&<p>暂无记录。收到新消息后会显示；暂停只停止页面刷新。</p>}{visible.map(r=><button key={r.id} onClick={()=>{setSelected(r.id);setDetail(null)}} className={`block w-full text-left border rounded p-3 ${r.id===selected?'border-green-500':''}`}><div>{new Date(r.time*1000).toLocaleTimeString()} · {r.status}</div><div className="text-sm break-all">{r.group||'后台任务'} · {r.sender}</div></button>)}</div>
      <div className="space-y-3 max-h-[70vh] overflow-auto">{selected&&!detail&&<p>记录已清空、已超过保留上限或正在加载。</p>}{!selected&&<p>选择一条记录查看完整流程。</p>}{detail?.events.map((e,i)=><details key={i} open className="border rounded p-3"><summary className="font-semibold">{e.stage} · +{e.elapsed} 秒</summary><pre className="whitespace-pre-wrap break-all text-sm mt-2">{e.detail}</pre></details>)}</div>
    </div>
  </div>
}
