import { useCallback, useEffect, useState } from 'react'

const API = 'http://127.0.0.1:7327'

export default function SendFailures() {
  const [page, setPage] = useState(1)
  const [status, setStatus] = useState('failed')
  const [data, setData] = useState({ items: [], total: 0, page_size: 20 })
  const [busyId, setBusyId] = useState(null)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/send-failures?page=${page}&page_size=20&status=${status}`)
      const body = await res.json()
      if (!body.ok) throw new Error(body.error || '读取失败')
      setData(body)
      setError('')
    } catch (err) {
      setError(err.message)
    }
  }, [page, status])

  useEffect(() => { load() }, [load])

  async function retry(id) {
    setBusyId(id)
    setError('')
    try {
      const res = await fetch(`${API}/api/send-failures/retry`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id }),
      })
      const body = await res.json()
      if (!body.ok) throw new Error(body.error || '重发未成功，请检查微信窗口')
      await load()
    } catch (err) {
      setError(err.message)
      await load()
    } finally {
      setBusyId(null)
    }
  }

  const pages = Math.max(1, Math.ceil(data.total / data.page_size))
  return (
    <div className="max-w-5xl space-y-5">
      <div className="flex items-center justify-between gap-4">
        <div>
          <h2 className="text-xl font-semibold">发送记录</h2>
          <p className="text-sm text-text-muted mt-1">仅记录最终发送失败的内容。重发需要微信窗口可用；“动作已执行”表示窗口发送操作完成，尚未核对微信实际送达。</p>
        </div>
        <select value={status} onChange={e => { setStatus(e.target.value); setPage(1) }}
          className="bg-bg-card border border-border-main rounded-xl px-3 py-2 text-sm">
          <option value="failed">待重发</option>
          <option value="uncertain">状态未知</option>
          <option value="sent">动作已执行</option>
          <option value="all">全部</option>
        </select>
      </div>
      {error && <p role="alert" className="text-[#d45656] text-sm">{error}</p>}
      <div className="space-y-3">
        {data.items.length === 0 && <p className="text-text-muted text-sm bg-bg-card border border-border-main rounded-xl p-6">暂无记录</p>}
        {data.items.map(item => (
          <div key={item.id} className="bg-bg-card border border-border-main rounded-xl p-4">
            <div className="flex items-center justify-between gap-3 mb-2">
              <div className="text-sm font-semibold break-all">{item.group_name}</div>
              <span className="text-xs text-text-muted whitespace-nowrap">{new Date(item.created_at * 1000).toLocaleString()}</span>
            </div>
            <p className="text-sm whitespace-pre-wrap break-words max-h-48 overflow-auto">{item.content}</p>
            <div className="flex items-center justify-between mt-3">
              <span className="text-xs text-text-muted">{item.status === 'sent' ? '发送动作已执行' : item.status === 'uncertain' ? '上次发送状态未知' : item.status === 'retrying' ? '正在重发' : '待重发'} · 重试 {item.attempts} 次</span>
              {(item.status === 'failed' || item.status === 'uncertain') && (
                <button disabled={busyId !== null} onClick={() => retry(item.id)}
                  className="bg-brand-green text-black rounded-lg px-4 py-1.5 text-sm font-semibold disabled:opacity-50 cursor-pointer">
                  {busyId === item.id ? '发送中…' : '重新发送'}
                </button>
              )}
            </div>
          </div>
        ))}
      </div>
      <div className="flex items-center justify-between text-sm">
        <span className="text-text-muted">共 {data.total} 条 · 第 {page}/{pages} 页</span>
        <div className="flex gap-2">
          <button disabled={page <= 1} onClick={() => setPage(page - 1)} className="px-3 py-1.5 rounded-lg border border-border-main disabled:opacity-40">上一页</button>
          <button disabled={page >= pages} onClick={() => setPage(page + 1)} className="px-3 py-1.5 rounded-lg border border-border-main disabled:opacity-40">下一页</button>
        </div>
      </div>
    </div>
  )
}
