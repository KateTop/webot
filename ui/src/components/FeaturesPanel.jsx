import { useState, useEffect } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { CheckCircle, Warning, FloppyDisk, Info } from '@phosphor-icons/react'
import { Field, Toggle, Select, Input } from './SharedComponents'
import WelcomeSection from './WelcomeEditor'

const pageTransition = {
  initial: { opacity: 0, x: 12 },
  animate: { opacity: 1, x: 0 },
  exit: { opacity: 0, x: -12 },
}

const paramPanel = {
  initial: { opacity: 0, y: 20 },
  animate: { opacity: 1, y: 0, transition: { duration: 0.3, ease: 'easeOut' } },
  exit: { opacity: 0, y: 20, transition: { duration: 0.2 } },
}

const sectionTitles = {
  summarize: '总结功能',
  proactive: '主动发言', sticky: '粘性提及', welcome: '欢迎新人', log: '日志级别',
}
const sectionAccents = {
  summarize: '#18E299',
  proactive: '#10b981', sticky: '#8b5cf6', welcome: '#f59e0b', log: '#6b7280',
}

// ── Helper ──────────────────────────────────────────────────────────

function ParamRow({ label, hint, children }) {
  return (
    <div>
      <p className="text-[14px] text-text-main font-medium">{label}</p>
      <p className="text-xs text-text-muted mt-0.5 mb-2">{hint}</p>
      {children}
    </div>
  )
}

// ── Summarize ───────────────────────────────────────────────────────

function SummarizeSection({ form, update }) {
  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <div className="flex-1 mr-8">
          <p className="text-[15px] text-text-main font-medium">总结功能</p>
          <p className="text-sm text-text-muted mt-1.5">@机器人 或 触发关键词时自动总结群聊内容</p>
        </div>
        <Toggle enabled={form.summarize_enabled} onChange={v => update('summarize_enabled', v)} />
      </div>
      <AnimatePresence>
        {form.summarize_enabled && (
          <motion.div variants={paramPanel} initial="initial" animate="animate" exit="exit"
            className="p-4 bg-bg-raised rounded-lg space-y-4">
            <ParamRow label="回溯时长" hint="触发总结时，至少拉取最近 N 小时的消息（默认 8，范围 1-72）">
              <Input type="number" value={String(form.fallback_window_hours || 8)}
                onChange={v => update('fallback_window_hours', Math.max(1, Math.min(72, parseInt(v) || 8)))} />
            </ParamRow>
            <div>
              <p className="text-[14px] text-text-main font-medium">触发关键词</p>
              <p className="text-xs text-text-muted mt-0.5 mb-2">群成员发送包含任一关键词的消息时触发总结。至少保留 1 个关键词。</p>
              <div className="flex flex-wrap gap-2 mb-2">
                {(form.trigger_keywords || []).map((kw, i) => (
                  <span key={i} className="inline-flex items-center gap-1 px-2.5 py-1 bg-brand-green-light border border-brand-green/20 rounded-lg text-[13px] text-brand-green-hover dark:text-brand-green">
                    {kw}
                    <button type="button" disabled={(form.trigger_keywords || []).length <= 1}
                      onClick={() => { const next = (form.trigger_keywords || []).filter((_, idx) => idx !== i); update('trigger_keywords', next) }}
                      className={`ml-0.5 leading-none text-base transition-colors ${(form.trigger_keywords || []).length <= 1 ? 'text-text-muted cursor-not-allowed' : 'text-brand-green-hover/60 hover:text-[#d45656] cursor-pointer'}`}>&times;</button>
                  </span>
                ))}
              </div>
              <div className="flex gap-2">
                <input type="text" id="kw-input" placeholder="输入新关键词，回车添加"
                  className="flex-1 bg-bg-raised border border-border-main rounded-lg px-3 py-2 text-[14px] text-text-main placeholder:text-text-muted/65 focus:outline-none focus:border-brand-green focus:ring-1 focus:ring-brand-green/15 transition-all"
                  onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); const val = e.target.value.trim(); if (val && !(form.trigger_keywords || []).includes(val)) { update('trigger_keywords', [...(form.trigger_keywords || []), val]); e.target.value = '' } } }} />
                <button type="button" onClick={() => { const el = document.getElementById('kw-input'); if (!el) return; const val = el.value.trim(); if (val && !(form.trigger_keywords || []).includes(val)) { update('trigger_keywords', [...(form.trigger_keywords || []), val]); el.value = '' } }}
                  className="px-4 py-2 bg-brand-green-light border border-brand-green/20 rounded-lg text-[13px] text-brand-green-hover dark:text-brand-green hover:bg-brand-green/10 transition-colors font-medium cursor-pointer">添加</button>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

// ── Proactive ───────────────────────────────────────────────────────

function ProactiveSection({ form, update }) {
  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <div className="flex-1 mr-8">
          <p className="text-[15px] text-text-main font-medium">主动发言</p>
          <p className="text-sm text-text-muted mt-1.5">无需 @提及，根据聊天活跃度自动参与对话</p>
        </div>
        <Toggle enabled={form.proactive_enabled} onChange={v => update('proactive_enabled', v)} />
      </div>
      <AnimatePresence>
        {form.proactive_enabled && (
          <motion.div variants={paramPanel} initial="initial" animate="animate" exit="exit"
            className="p-4 bg-bg-raised rounded-lg space-y-3">
            <p className="text-xs text-text-muted leading-relaxed">
              机器人统计最近 <strong>速率窗口</strong> 秒内的群聊消息速率（条/分钟），与下方四个阈值比较，决定发言频率：
            </p>
            <div className="grid grid-cols-2 gap-3">
              <ParamRow label="速率窗口" hint="统计消息速率的时间范围（秒），默认 120 = 2 分钟">
                <Input type="number" value={String(form.proactive_rate_window_sec || 120)}
                  onChange={v => update('proactive_rate_window_sec', parseInt(v) || 120)} />
              </ParamRow>
              <ParamRow label="安静阈值" hint="速率低于此值不发言（默认 1.5 条/分）">
                <Input type="number" value={String(form.proactive_rate_quiet ?? 1.5)}
                  onChange={v => update('proactive_rate_quiet', parseFloat(v) || 1.5)} />
              </ParamRow>
              <ParamRow label="随口阈值" hint="超过此值偶尔插话（默认 4.0 条/分）">
                <Input type="number" value={String(form.proactive_rate_casual ?? 4.0)}
                  onChange={v => update('proactive_rate_casual', parseFloat(v) || 4.0)} />
              </ParamRow>
              <ParamRow label="活跃阈值" hint="超过此值频繁参与（默认 6.5 条/分）">
                <Input type="number" value={String(form.proactive_rate_lively ?? 6.5)}
                  onChange={v => update('proactive_rate_lively', parseFloat(v) || 6.5)} />
              </ParamRow>
              <ParamRow label="爆发阈值" hint="超过此值火力全开（默认 8.5 条/分）">
                <Input type="number" value={String(form.proactive_rate_burst ?? 8.5)}
                  onChange={v => update('proactive_rate_burst', parseFloat(v) || 8.5)} />
              </ParamRow>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

// ── Sticky Mention ──────────────────────────────────────────────────

function StickySection({ form, update }) {
  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <div className="flex-1 mr-8">
          <p className="text-[15px] text-text-main font-medium">粘性提及</p>
          <p className="text-sm text-text-muted mt-1.5">@机器人后无需等待回复即可继续说，机器人会追踪后续消息</p>
        </div>
        <Toggle enabled={form.sticky_mention_enabled} onChange={v => update('sticky_mention_enabled', v)} />
      </div>
      <AnimatePresence>
        {form.sticky_mention_enabled && (
          <motion.div variants={paramPanel} initial="initial" animate="animate" exit="exit"
            className="p-4 bg-bg-raised rounded-lg">
            <ParamRow label="追踪超时" hint="用户发送空 @消息后，等待后续消息的最长时间">
              <Select value={String(form.sticky_mention_ttl_sec || 60) + ' 秒'}
                onChange={v => update('sticky_mention_ttl_sec', parseInt(v))}
                options={[
                  { value: '30 秒', desc: '快速响应 (30 秒)', hint: '30 秒后自动失效' },
                  { value: '60 秒', desc: '默认 (60 秒)', hint: '60 秒后自动失效' },
                  { value: '120 秒', desc: '宽松 (120 秒)', hint: '120 秒后自动失效' },
                  { value: '300 秒', desc: '最长时间 (300 秒)', hint: '300 秒后自动失效' },
                ]} />
            </ParamRow>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

// ── Welcome (imported from ConfigPanel) ─────────────────────────────
// WelcomeSection, AddGroupPicker, SmallSelect are too large to inline.
// We import them lazily from ConfigPanel.

// ── Log Level ───────────────────────────────────────────────────────

function LogSection({ form, update }) {
  return (
    <div>
      <Field label="日志级别" hint="记录机器人运行日志的详细程度">
        <Select value={form.log_level} onChange={v => update('log_level', v)} options={[
          { value: 'DEBUG', desc: '调试信息', hint: '排查故障时使用' },
          { value: 'INFO', desc: '常规信息', hint: '日常使用（推荐）' },
          { value: 'WARNING', desc: '仅警告', hint: '长期稳定运行时使用' },
          { value: 'ERROR', desc: '仅错误', hint: '只关心故障时使用' },
        ]} />
      </Field>
    </div>
  )
}

// ── Main FeaturesPanel ──────────────────────────────────────────────

export default function FeaturesPanel({ activeSection, onNavigate }) {
  const [saved, setSaved] = useState(false)
  const [saveError, setSaveError] = useState('')
  const [loaded, setLoaded] = useState(false)
  const [form, setForm] = useState({
    summarize_enabled: true, fallback_window_hours: 8, trigger_keywords: [],
    proactive_enabled: false, proactive_rate_window_sec: 120,
    proactive_rate_quiet: 1.5, proactive_rate_casual: 4.0,
    proactive_rate_lively: 6.5, proactive_rate_burst: 8.5,
    sticky_mention_enabled: true, sticky_mention_ttl_sec: 60,
    welcome_enabled: false,
    log_level: 'INFO',
  })

  useEffect(() => {
    async function load() {
      try {
        const res = await fetch('http://127.0.0.1:7327/api/load-config')
        const data = await res.json()
        if (!res.ok || !data.ok || !data.config) throw new Error('读取配置失败')
        setForm(prev => ({ ...prev, ...data.config }))
        setLoaded(true)
      } catch {
        setSaveError('读取已有配置失败，请刷新页面后重试')
      }
    }
    load()
  }, [])

  function update(key, value) { setForm(prev => ({ ...prev, [key]: value })); setSaved(false); setSaveError('') }

  async function handleSave() {
    if (!loaded) return
    setSaved(false); setSaveError('')
    try {
      const res = await fetch('http://127.0.0.1:7327/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          summarize_enabled: form.summarize_enabled, fallback_window_hours: form.fallback_window_hours,
          trigger_keywords: form.trigger_keywords,
          proactive_enabled: form.proactive_enabled, proactive_rate_window_sec: form.proactive_rate_window_sec,
          proactive_rate_quiet: form.proactive_rate_quiet, proactive_rate_casual: form.proactive_rate_casual,
          proactive_rate_lively: form.proactive_rate_lively, proactive_rate_burst: form.proactive_rate_burst,
          sticky_mention_enabled: form.sticky_mention_enabled, sticky_mention_ttl_sec: form.sticky_mention_ttl_sec,
          welcome_enabled: form.welcome_enabled,
          log_level: form.log_level,
        }),
      })
      const data = await res.json()
      if (data.ok) { setSaved(true); setTimeout(() => setSaved(false), 3000) }
      else { setSaveError(data.error || '保存失败'); setTimeout(() => setSaveError(''), 5000) }
    } catch { setSaveError('无法连接到服务器，请确认机器人已启动'); setTimeout(() => setSaveError(''), 5000) }
  }

  return (
    <div className="max-w-2xl">
      <AnimatePresence>
        {saved && (
          <motion.div initial={{ opacity: 0, y: -8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -8 }}
            className="mb-4 flex items-center gap-2 px-5 py-2.5 bg-brand-green-light border border-brand-green/20 rounded-full text-sm text-brand-green-hover dark:text-brand-green font-medium shadow-sm">
            <CheckCircle size={18} weight="fill" /> 配置已保存。需要重启机器人才能生效。
          </motion.div>
        )}
        {saveError && (
          <motion.div initial={{ opacity: 0, y: -8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -8 }}
            className="mb-4 flex items-center gap-2 px-5 py-2.5 bg-[#d45656]/5 border border-[#d45656]/20 rounded-full text-sm text-[#d45656] font-medium shadow-sm">
            <Warning size={18} weight="fill" /> {saveError}
          </motion.div>
        )}
      </AnimatePresence>

      <div style={{ minHeight: 420 }}>
        <AnimatePresence mode="wait">
          <motion.div key={activeSection} variants={pageTransition} initial="initial" animate="animate" exit="exit" transition={{ duration: 0.18 }}>
            <div className="flex items-center gap-2.5 mb-5 pl-1">
              <div className="w-1.5 h-4.5 rounded-full shadow-sm" style={{ backgroundColor: sectionAccents[activeSection] }} />
              <h3 className="text-sm font-semibold tracking-tight text-text-main">{sectionTitles[activeSection]}</h3>
            </div>
            <div className="bg-bg-card border border-border-main rounded-2xl shadow-[rgba(0,0,0,0.03)_0px_2px_4px] dark:shadow-none">
              <div className="p-7">
                {activeSection === 'summarize' && <SummarizeSection form={form} update={update} />}
                {activeSection === 'proactive' && <ProactiveSection form={form} update={update} />}
                {activeSection === 'sticky' && <StickySection form={form} update={update} />}
                {activeSection === 'welcome' && <WelcomeSection form={form} update={update} />}
                {activeSection === 'log' && <LogSection form={form} update={update} />}
              </div>
            </div>
          </motion.div>
        </AnimatePresence>
      </div>

      {activeSection !== 'log' && (
        <div className="mt-8 flex items-center gap-4">
          <motion.button whileTap={{ scale: 0.97 }} whileHover={{ scale: 1.02 }} onClick={handleSave} disabled={!loaded}
            className={`w-48 py-2.5 rounded-full text-[14px] font-semibold tracking-wide shadow-sm transition-all duration-300 flex items-center justify-center gap-2 cursor-pointer ${saved ? 'bg-brand-green-light border border-brand-green/20 text-brand-green-hover dark:text-brand-green font-semibold' : 'bg-[#0d0d0d] dark:bg-white text-white dark:text-[#0d0d0d] border border-[#0d0d0d] dark:border-border-main hover:opacity-90'}`}>
            {saved ? <><CheckCircle size={18} weight="fill" /> 已保存</> : <><FloppyDisk size={18} /> 保存配置</>}
          </motion.button>
          {saved ? (
            <span className="flex items-center gap-1.5 text-xs text-[#c37d0d] bg-[#c37d0d]/10 border border-[#c37d0d]/20 px-4 py-1.5 rounded-full font-medium">
              <Info size={14} /> 配置已更新，重启机器人后生效
            </span>
          ) : (
            <span className="flex items-center gap-1.5 text-xs text-text-muted bg-bg-raised border border-border-main px-4 py-1.5 rounded-full font-medium">
              <Info size={14} /> 保存将应用所有模块的修改，重启后生效
            </span>
          )}
        </div>
      )}
    </div>
  )
}
