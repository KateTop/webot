export const PROACTIVE_FIELDS = [
          ['proactive_min_messages', '主动发言最少消息', 2, 100],
          ['proactive_min_participants', '主动发言最少人数', 1, 20],
          ['proactive_min_age_sec', '话题最短持续（秒）', 0, 600],
          ['proactive_min_new_messages', '两次发言间新增消息', 1, 100],
          ['proactive_cooldown_sec', '主动发言冷却（秒）', 30, 7200],
          ['proactive_resume_gap_sec', '停顿后重新展开话题的间隔（秒）', 30, 3600],
          ['proactive_daily_limit', '每群每日主动发言上限（0 禁用）', 0, 100],
          ['proactive_eval_sec', '候选判断间隔（秒）', 30, 7200],
          ['proactive_context_count', '主动发言上下文条数', 10, 100],
          ['proactive_max_chars', '主动发言最大字数（超长不发）', 20, 160],
          ['proactive_quiet_start', '安静时段开始（小时）', 0, 23],
          ['proactive_quiet_end', '安静时段结束（相同则关闭）', 0, 23],
          ['proactive_feedback_sec', '观察接话时间（秒）', 60, 3600],
          ['proactive_feedback_messages', '观察接话消息数', 1, 30],
          ['proactive_ignored_limit', '连续无人接话多少次后冷却翻倍', 1, 10],
          ['proactive_max_replies', '单段最多主动发言', 0, 10],
]
export const PROACTIVE_KEYS = [...PROACTIVE_FIELDS.map(([key]) => key), 'proactive_phases', 'proactive_allowed_moves', 'proactive_trigger_words']
export function proactivePatch(policy) {
  return Object.fromEntries(PROACTIVE_KEYS.map(key => [key, policy[key]]))
}
export default function ProactivePolicy({policy, setPolicy}) {
  if (!policy) return <p>正在读取主动发言策略…</p>
  return <div className="space-y-5">
    <p className="text-xs text-text-muted leading-relaxed">先按话题、参与人数和发言节奏筛选，再由 AI 判断是否有值得补充的内容；通过规则也可能保持沉默。</p>
    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
      {PROACTIVE_FIELDS.map(([key,label,min,max]) => <label key={key} className="text-sm space-y-1">
        <span>{label}</span><input type="number" min={min} max={max} step={1} value={policy[key]}
          onChange={e=>setPolicy(prev=>({...prev,[key]:e.target.value === '' ? '' : Number(e.target.value)}))}
          className="w-full bg-bg-raised border border-border-main rounded-lg px-3 py-2" />
      </label>)}
    </div>
      <label className="text-sm block space-y-1"><span>允许主动发言的阶段</span>
        <select value={policy.proactive_phases} onChange={e => setPolicy(prev => ({ ...prev, proactive_phases: e.target.value }))}
          className="w-full bg-bg-raised border border-border-main rounded-lg px-3 py-2">
          <option value="rising_peak">升温和持续讨论（推荐）</option><option value="all_active">包括降温阶段</option>
        </select></label>
      <label className="text-sm block space-y-1"><span>允许的插话方式</span>
        <input value={policy.proactive_allowed_moves} maxLength={200}
          onChange={e => setPolicy(prev => ({ ...prev, proactive_allowed_moves: e.target.value }))}
          className="w-full bg-bg-raised border border-border-main rounded-lg px-3 py-2" /></label>
      <label className="text-sm block space-y-1"><span>候选话题关键词（用顿号分隔，仅预筛，AI仍可保持沉默）</span>
        <input value={policy.proactive_trigger_words} maxLength={200}
          onChange={e => setPolicy(prev => ({ ...prev, proactive_trigger_words: e.target.value }))}
          className="w-full bg-bg-raised border border-border-main rounded-lg px-3 py-2" /></label>
      <p className="text-xs text-text-muted leading-relaxed">推荐先用“追问、带细节的回应”。每日额度及冷却按群保存，切段和重启不会重置；确认在微信库中发出后占额度；未确认不会自动重复发送。未观察到明确接话不代表反感。参数保存立即生效，人设与主动发言指令在 AI 配置中编辑。</p>
  </div>
}
