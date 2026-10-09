import { PROACTIVE_KEYS } from './ProactivePolicy'
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
  const [versionToken, setVersionToken] = useState(null)
  const [needsMigration, setNeedsMigration] = useState(false)
  const [migrationText, setMigrationText] = useState('')
  const [snapshots, setSnapshots] = useState([])
  const [blockText, setBlockText] = useState('[]')
  const [settlePrompt, setSettlePrompt] = useState('')
  const [reminders, setReminders] = useState([])
  const [deliveries, setDeliveries] = useState([])
  const [audit, setAudit] = useState([])
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
    request('/prompts').then(data => { setPrompt(data.prompts?.memory || ''); setSettlePrompt(data.prompts?.settle || '') })
      .catch(e => setError(e.message))
    request('/conversation-policy').then(data => setPolicy(data.policy))
      .catch(e => setError(e.message))
  }, [])

  async function refreshGroup(id = chatId) {
    if (!id) return
    const data = await request(`/memory?chat_id=${encodeURIComponent(id)}`)
    setMemory(data.memory)
    setVersionToken(data.token)
    setReminders(data.reminders || [])
    setDeliveries(data.deliveries || [])
    setAudit(data.audit || [])
    setNeedsMigration(data.needs_migration)
    setMigrationText(data.migration_preview || '')
    setSnapshots(data.snapshots || [])
    setBlockText(JSON.stringify(data.blocks || [], null, 2))
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
        await request('/memory/save', { chat_id: chatId, text, token: versionToken }); await refreshGroup()
        setSavedSoul(text)
        setSoulExists(true)
        setMessage('soul.md 已保存；下一次问答或整理即可读取')
      } else if (action === 'prompt') {
        await request('/prompts', { memory: prompt, settle: settlePrompt })
        setMessage('记忆整理指令已保存；下一次整理立即生效')
      } else if (action === 'migrate') {
        await request('/memory/migrate', { chat_id: chatId, text: migrationText, token: versionToken })
        await refreshGroup(); setMessage('迁移已应用，旧正文已备份；请检查人物归属，迁移不会猜测身份。')
      } else if (action === 'blocks') {
        await request('/memory/blocks', { chat_id: chatId, blocks: JSON.parse(blockText) })
        setMessage('不再记录清单已保存；原始聊天库不受影响。')
      } else if (action === 'settle') {
        const data = await request('/memory/settle', { chat_id: chatId })
        setJobId(data.job_id); return
      } else if (action === 'create') {
        await request('/memory/create', { chat_id: chatId })
        await refreshGroup()
        setMessage('已创建该群的 soul.md，可直接编辑并保存')
      } else if (action === 'policy') {
        const data = await request('/conversation-policy', Object.fromEntries(Object.entries(policy).filter(([key]) => !PROACTIVE_KEYS.includes(key))))
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
        ['activity', '提醒与发送状态'], ['versions', '迁移与版本'], ['blocks', '不再记录'], ['prompt', '记忆整理指令'], ['policy', '对话策略'], ['nicknames', '群友昵称'],
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

    {section === 'activity' && <div className="bg-bg-card border border-border-main rounded-2xl p-6 space-y-4">
      <h3>获准的提醒与发送状态</h3><p className={muted}>提醒只接受艾特/引用中的明确请求，例如“提醒我 2026-10-10 12:00 喂猫”，确认消息在微信库中出现后才启用。每30秒检查，未确认不自动重发。</p>
      <button className={button} onClick={() => refreshGroup().catch(e => setError(e.message))}>刷新状态</button>
      {reminders.map(item => <div key={item.id} className="border rounded-xl p-3"><p>{stamp(item.due)} · {item.status} · {item.text}</p>
        {item.status === 'scheduled' && <button onClick={async () => {try { await request('/memory/reminder-cancel',{chat_id:chatId,id:item.id}); await refreshGroup() } catch(e) {setError(e.message)} }}>取消提醒</button>}</div>)}
      <h4>最近发送确认</h4>{deliveries.map(item => <p key={item.id}>{stamp(item.created_at)} · {item.action || '问答/提醒'} · {{confirmed:'微信库已确认',unconfirmed:'未确认，不自动重发',failed:'发送操作失败'}[item.status] || item.status}</p>)}
      <h4>记忆操作记录</h4>{audit.map((item,index) => <p key={index}>{stamp(item.created_at)} · {item.operations}</p>)}
    </div>}
    {section === 'versions'  && <div className="bg-bg-card border border-border-main rounded-2xl p-6 space-y-4">
      <h3>迁移预览与版本历史</h3>
      <p className={muted}>旧记忆先预览，再应用。迁移保留措辞，未知人物归属留空；请核对 subject_id。ID 与元数据应保留。应用前备份旧正文，最近保留5版。</p>
      <textarea rows={14} value={migrationText} onChange={e => setMigrationText(e.target.value)} className="w-full bg-bg-raised rounded-xl p-3" />
      <button className={button} disabled={working || !chatId} onClick={() => act('migrate')}>{needsMigration ? '确认应用迁移并备份' : '保存所审阅的结构'}</button>
      {snapshots.map(item => <div key={item.id} className="flex justify-between"><span>版本 {item.version} · {stamp(item.created_at)}</span>
        <button disabled={working} onClick={async () => { try { await request('/memory/restore', {chat_id:chatId,snapshot_id:item.id}); await refreshGroup() } catch(e) {setError(e.message)} }}>恢复此版本</button></div>)}
    </div>}
    {section === 'blocks' && <div className="bg-bg-card border border-border-main rounded-2xl p-6 space-y-4">
      <h3>不再记录清单</h3><p className={muted}>仅保存成员 ID 与粗粒度主题，不保留被忘记的具体细节。原始聊天库仍保留。移除规则会允许未来重新提取；不会恢复已清除的快照。</p>
      <textarea rows={10} value={blockText} onChange={e => setBlockText(e.target.value)} className="w-full bg-bg-raised rounded-xl p-3" />
      <button className={button} disabled={working} onClick={() => act('blocks')}>保存清单</button>
    </div>}
    {section === 'pending'  && <div className="bg-bg-card border border-border-main rounded-2xl p-6 space-y-4">
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
      <p className={muted}>支持 {'{name}'}、{'{base}'}、{'{persona}'}、{'{soul}'}、{'{today}'}、{'{history}'}、{'{max_chars}'}。写入返回 NO_UPDATE 或 changes JSON，沉淀返回 NO_UPDATE 或 operations JSON；空白响应不视为成功。</p>
      <textarea aria-label="记忆整理指令" value={prompt} onChange={e => setPrompt(e.target.value)} rows={12}
        className="w-full bg-bg-raised border border-border-main rounded-xl p-4 text-sm text-text-main focus:outline-none focus:border-brand-green" placeholder="例如：重要事件保留时间与依据；不要把玩笑写成稳定的人物判断。" />
      <p className={muted}>强制协议：无更新只返回 NO_UPDATE；空白是失败，不推进游标。写入引用消息ID，日期由程序计算。每批最多100条。</p>
      <h4>夜间沉淀补充指令</h4><textarea rows={8} value={settlePrompt} onChange={e => setSettlePrompt(e.target.value)} className="w-full bg-bg-raised rounded-xl p-3" />
      <button className={button} disabled={working} onClick={() => act('prompt')}>保存整理指令</button>
      <button className={button} disabled={working || needsMigration} onClick={() => act('settle')}>现在执行沉淀</button>
    </div>}

    {section === 'policy' && policy && <div className="bg-bg-card border border-border-main rounded-2xl p-6 space-y-5">
      <div><h3 className="font-semibold">对话与记忆策略</h3>
        <p className={muted}>设置话题分段、记忆整理及通用回复限制。主动发言的专用参数在“功能开关 → 主动发言”管理。</p></div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {[
          ['episode_gap_sec', '话题间隔（秒）', 60, 3600],
          ['episode_max_messages', '单段最多消息', 20, 100],
          ['topic_min_messages', '话题变化前最少消息', 3, 50],
          ['topic_similarity', '话题相似度阈值（0–0.8）', 0, 0.8],
          ['memory_settle_sec', '沉寂后整理（秒）', 60, 3600],
          ['memory_body_max_chars', '记忆正文上限（不含ID与证据）', 500, 10000],
          ['memory_min_messages', '自动整理最少有效消息', 2, 100],
          ['memory_fallback_sec', '慢速群整理兜底（秒）', 3600, 86400],
          ['memory_tail_sec', '兜底保留最近尾巴（秒）', 60, 1800],
          ['mention_per_minute', '每人每分钟艾特/引用上限', 1, 60],
          ['send_delay_min_sec', '发送延迟下限（秒）', 0, 10],
          ['send_delay_max_sec', '发送延迟上限（秒）', 0, 10],
        ].map(([key, label, min, max]) => <label key={key} className="text-sm space-y-1">
          <span>{label}</span><input type="number" min={min} max={max} step={key === 'topic_similarity' ? 0.01 : 1}
            value={policy[key]} onChange={e => setPolicy(prev => ({ ...prev, [key]: Number(e.target.value) }))}
            className="w-full bg-bg-raised border border-border-main rounded-lg px-3 py-2" /></label>)}
      </div>
      <p className={muted}>主动发言参数已统一移至“功能开关 → 主动发言”。此处话题分段规则同时供记忆整理和主动发言使用。</p>
      <button className={button} disabled={working} onClick={() => act('policy')}>保存对话策略</button>
    </div>}

    {section === 'nicknames' && chatId && <NicknameEditor groupFromParent={chatId} hideGroupSelector />}
  </div>
}
