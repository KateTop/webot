import { useEffect, useState } from 'react'
import NicknameEditor from './NicknameEditor'

const API = 'http://127.0.0.1:7327/api'
const stamp = value => value ? new Date(value * 1000).toLocaleString('zh-CN', { hour12: false }) : '—'

async function request(path, body) {
  const response = await fetch(`${API}${path}`, body === undefined ? undefined : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })
  const data = await response.json()
  if (!response.ok || !data.ok) throw new Error(data.error || '操作失败')
  return data
}

export default function MemoryPanel() {
  const [groups, setGroups] = useState([])
  const [chatId, setChatId] = useState('')
  const [section, setSection] = useState('soul')
  const [memory, setMemory] = useState(null)
  const [soul, setSoul] = useState('')
  const [savedSoul, setSavedSoul] = useState('')
  const [soulPath, setSoulPath] = useState('')
  const [soulExists, setSoulExists] = useState(false)
  const [pending, setPending] = useState({ segments: [], has_more: false })
  const [prompt, setPrompt] = useState('')
  const [policy, setPolicy] = useState(null)
  const [preview, setPreview] = useState(null)
  const [working, setWorking] = useState(false)
  const [jobId, setJobId] = useState('')
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    request('/memory/groups').then(data => {
      setGroups(data.groups || [])
      if (data.groups?.length) setChatId(data.groups[0].chat_id)
    }).catch(e => setError(e.message))
    request('/prompts').then(data => setPrompt(data.prompts?.memory || ''))
      .catch(e => setError(e.message))
    request('/conversation-policy').then(data => setPolicy(data.policy))
      .catch(e => setError(e.message))
  }, [])

  async function refreshGroup(id = chatId) {
    if (!id) return
    const data = await request(`/memory?chat_id=${encodeURIComponent(id)}`)
    setMemory(data.memory)
    setSoul(data.memory?.memory_text || '')
    setSavedSoul(data.memory?.memory_text || '')
    setSoulPath(data.soul_path || '')
    setSoulExists(Boolean(data.soul_exists))
    setPending(data.pending || { segments: [] })
    setPreview(null)
  }

  useEffect(() => {
    setError(''); setMessage(''); setMemory(null); setPreview(null)
    if (chatId) refreshGroup(chatId).catch(e => setError(e.message))
  }, [chatId])

  useEffect(() => {
    if (!jobId) return
    let cancelled = false
    const timer = setInterval(async () => {
      try {
        const data = await request(`/memory/jobs?id=${encodeURIComponent(jobId)}`)
        if (cancelled || data.job?.state === 'running') return
        clearInterval(timer); setJobId(''); setWorking(false)
        if (data.job?.state === 'done') {
          setMessage('此时间段已整理进 soul.md')
          await refreshGroup()
        } else setError(data.job?.error || '整理失败')
      } catch (e) { if (!cancelled) { clearInterval(timer); setJobId(''); setWorking(false); setError(e.message) } }
    }, 1500)
    return () => { cancelled = true; clearInterval(timer) }
  }, [jobId, chatId])

  async function act(action) {
    setWorking(true); setError(''); setMessage('')
    try {
      if (action === 'soul') {
        const text = soul
        await request('/memory/save', { chat_id: chatId, text })
        setSavedSoul(text)
        setSoulExists(true)
        setMessage('soul.md 已保存；下一次问答或整理即可读取')
      } else if (action === 'prompt') {
        await request('/prompts', { memory: prompt })
        setMessage('记忆整理指令已保存；下一次整理立即生效')
      } else if (action === 'create') {
        await request('/memory/create', { chat_id: chatId })
        await refreshGroup()
        setMessage('已创建该群的 soul.md，可直接编辑并保存')
      } else if (action === 'policy') {
        const data = await request('/conversation-policy', policy)
        setPolicy(data.policy)
        setMessage('对话与记忆策略已保存；下一段对话起生效')
      }
    } catch (e) { setError(e.message) }
    finally { setWorking(false) }
  }

  async function showSegment(segment) {
    setError('')
    try {
      const query = `chat_id=${encodeURIComponent(chatId)}&start_id=${segment.start_id}&end_id=${segment.end_id}`
      const data = await request(`/memory/segment?${query}`)
      setPreview({ endId: segment.end_id, messages: data.messages || [] })
    } catch (e) { setError(e.message) }
  }

  async function consolidate(segment) {
    setWorking(true); setError(''); setMessage('')
    try {
      const data = await request('/memory/consolidate', { chat_id: chatId, end_id: segment.end_id })
      setJobId(data.job_id)
      setMessage('正在使用记忆整理指令处理最早的时间段…')
    } catch (e) { setWorking(false); setError(e.message) }
  }

  const button = 'px-4 py-2 rounded-lg bg-brand-green text-[#07140f] text-sm font-semibold disabled:opacity-40 hover:opacity-90'
  const muted = 'text-sm text-text-muted'
  return <div className="max-w-5xl space-y-5">
    <div className="bg-bg-card border border-border-main rounded-2xl p-5">
      <div className="flex flex-wrap items-center gap-3">
        <label className="text-sm font-semibold">选择群聊</label>
        <select value={chatId} disabled={working} onChange={e => {
          if (soul !== savedSoul && !window.confirm('当前 soul.md 尚未保存，确定切换群聊吗？')) return
          setChatId(e.target.value)
        }} className="min-w-64 flex-1 bg-bg-raised border border-border-main rounded-lg px-3 py-2 text-sm">
          {groups.map(group => <option key={group.chat_id} value={group.chat_id}>{group.group_name}</option>)}
        </select>
        <button className="text-sm text-brand-green" onClick={() => {
          if (soul !== savedSoul && !window.confirm('当前 soul.md 尚未保存，确定刷新吗？')) return
          refreshGroup().catch(e => setError(e.message))
        }}>刷新</button>
      </div>
      <p className="mt-3 text-xs text-text-muted">每群独立保存 soul.md；聊天原文仍保留在 SQLite。手动整理按时间顺序推进，不会跳过未处理消息。</p>
    </div>

    <div className="flex flex-wrap gap-2">
      {[
        ['soul', '群聊 soul.md'], ['pending', '待整理对话'],
        ['prompt', '记忆整理指令'], ['policy', '对话策略'], ['nicknames', '群友昵称'],
      ].map(([key, label]) => <button key={key} onClick={() => setSection(key)}
        className={`px-4 py-2 rounded-full text-sm ${section === key ? 'bg-brand-green-light text-brand-green font-semibold' : 'bg-bg-raised text-text-muted'}`}>{label}</button>)}
    </div>
    {error && <div className="p-3 rounded-lg bg-red-500/10 text-red-500 text-sm">{error}</div>}
    {message && <div className="p-3 rounded-lg bg-brand-green-light text-brand-green text-sm">{message}</div>}

    {section === 'soul' && <div className="bg-bg-card border border-border-main rounded-2xl p-6 space-y-4">
      <div>
        <h3 className="font-semibold">当前群聊记忆</h3>
        <p className={muted}>已整理 {memory?.message_count || 0} 条；上次整理 {stamp(memory?.last_consolidated)}。手动编辑不会改变已整理游标。</p>
        {soulPath && <p className="text-xs text-text-muted mt-1 break-all">文件：{soulPath}（{soulExists ? '已创建' : '尚未创建'}）</p>}
      </div>
      {!soulExists && <button className={button} disabled={!chatId || working} onClick={() => act('create')}>创建空的 soul.md</button>}
      <textarea aria-label="群聊 soul.md" value={soul} onChange={e => setSoul(e.target.value)} rows={18}
        className="w-full bg-bg-raised border border-border-main rounded-xl p-4 text-sm text-text-main focus:outline-none focus:border-brand-green" placeholder="还没有记忆。可以先写下这个群的已知背景，也可以从待整理对话开始生成。" />
      <button className={button} disabled={!chatId || working || soul === savedSoul} onClick={() => act('soul')}>保存 soul.md</button>
    </div>}

    {section === 'pending' && <div className="bg-bg-card border border-border-main rounded-2xl p-6 space-y-4">
      <div><h3 className="font-semibold">聊天库中尚未整理的对话</h3>
        <p className={muted}>按间隔、条数和连续消息确认的话题变化分段；参数可在“对话策略”调整。为保护游标，只能先整理最早一段。</p></div>
      {(pending.segments || []).length === 0 && <p className={muted}>当前没有待整理消息。</p>}
      {(pending.segments || []).map((segment, index) => <div key={segment.end_id} className="border border-border-main rounded-xl p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div><p className="text-sm font-medium">{stamp(segment.start_time)} → {stamp(segment.end_time)}</p>
            <p className="text-xs text-text-muted">{segment.count} 条 · {segment.closed ? '已结束' : '进行中'} · 边界：{segment.end_reason} · {segment.participants} 人</p>
            {segment.preview && <p className="text-xs text-text-muted mt-1">起始消息：{segment.preview}</p>}</div>
          <div className="flex gap-2">
            <button className="px-3 py-2 rounded-lg border border-border-main text-sm" onClick={() => showSegment(segment)}>查看对话</button>
            <button className={button} disabled={index !== 0 || working} onClick={() => consolidate(segment)}>整理到记忆</button>
          </div>
        </div>
        {preview?.endId === segment.end_id && <div className="mt-4 max-h-72 overflow-auto space-y-2 border-t border-border-main pt-3">
          {preview.messages.map(item => <div key={item.id} className="text-sm"><span className="text-text-muted">{stamp(item.timestamp)} {item.sender_name}：</span>{item.content}</div>)}
        </div>}
      </div>)}
      {pending.has_more && <p className={muted}>仅展示前 5000 条未整理消息组成的时间段；整理后刷新可继续查看。</p>}
    </div>}

    {section === 'prompt' && <div className="bg-bg-card border border-border-main rounded-2xl p-6 space-y-4">
      <h3 className="font-semibold">记忆创建与沉淀指令</h3>
      <p className={muted}>这里的内容追加到内置记忆整理规则后。自动整理、启动补录和手动整理使用同一份指令；留空时使用内置规则。</p>
      <textarea aria-label="记忆整理指令" value={prompt} onChange={e => setPrompt(e.target.value)} rows={12}
        className="w-full bg-bg-raised border border-border-main rounded-xl p-4 text-sm text-text-main focus:outline-none focus:border-brand-green" placeholder="例如：重要事件保留时间与依据；不要把玩笑写成稳定的人物判断。" />
      <button className={button} disabled={working} onClick={() => act('prompt')}>保存整理指令</button>
    </div>}

    {section === 'policy' && policy && <div className="bg-bg-card border border-border-main rounded-2xl p-6 space-y-5">
      <div><h3 className="font-semibold">对话与记忆策略</h3>
        <p className={muted}>本地规则判断话题范围、热度和沉淀时机；只有进入整理或候选发言时才调用 AI。参数保存到 data/conversation_policy.json。</p></div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {[
          ['episode_gap_sec', '话题间隔（秒）', 60, 3600],
          ['episode_max_messages', '单段最多消息', 20, 100],
          ['topic_min_messages', '话题变化前最少消息', 3, 50],
          ['topic_similarity', '话题相似度阈值（0–0.8）', 0, 0.8],
          ['memory_settle_sec', '沉寂后整理（秒）', 60, 3600],
          ['memory_min_messages', '自动整理最少消息', 2, 100],
          ['proactive_min_messages', '主动发言最少消息', 2, 100],
          ['proactive_min_participants', '主动发言最少人数', 1, 20],
          ['proactive_min_age_sec', '话题最短持续（秒）', 0, 600],
          ['proactive_min_new_messages', '两次发言间新增消息', 1, 100],
          ['proactive_cooldown_sec', '主动发言冷却（秒）', 30, 7200],
          ['proactive_max_replies', '单段最多主动发言', 0, 10],
        ].map(([key, label, min, max]) => <label key={key} className="text-sm space-y-1">
          <span>{label}</span><input type="number" min={min} max={max} step={key === 'topic_similarity' ? 0.01 : 1}
            value={policy[key]} onChange={e => setPolicy(prev => ({ ...prev, [key]: Number(e.target.value) }))}
            className="w-full bg-bg-raised border border-border-main rounded-lg px-3 py-2" /></label>)}
      </div>
      <label className="text-sm block space-y-1"><span>允许主动发言的阶段</span>
        <select value={policy.proactive_phases} onChange={e => setPolicy(prev => ({ ...prev, proactive_phases: e.target.value }))}
          className="w-full bg-bg-raised border border-border-main rounded-lg px-3 py-2">
          <option value="rising_peak">升温和持续讨论（推荐）</option><option value="all_active">包括降温阶段</option>
        </select></label>
      <button className={button} disabled={working} onClick={() => act('policy')}>保存对话策略</button>
    </div>}

    {section === 'nicknames' && chatId && <NicknameEditor groupFromParent={chatId} hideGroupSelector />}
  </div>
}
