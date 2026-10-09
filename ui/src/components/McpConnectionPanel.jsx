import { useState, useEffect } from 'react'
import { Field, Input } from './SharedComponents'
export default function McpConnectionPanel() {
  const [form,setForm] = useState(null), [message,setMessage] = useState(''), [saving,setSaving] = useState(false)
  useEffect(()=>{fetch('http://127.0.0.1:7327/api/mcp-config').then(r=>r.json()).then(d=>{if(d.ok)setForm(d.config);else setMessage('读取MCP配置失败')}).catch(()=>setMessage('读取MCP配置失败'))},[])
  async function save() {
    setSaving(true)
    try {const d=await (await fetch('http://127.0.0.1:7327/api/mcp-config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(form)})).json();if(!d.ok)throw new Error(d.error);setForm(d.config);setMessage('MCP连接已保存，停止并重新启动机器人后生效。')}
    catch(e){setMessage(e.message)}finally{setSaving(false)}
  }
  return <div className="border border-border-main rounded-xl p-4 space-y-4">
    <p className="text-sm">WeChatDataAnalysis MCP：需要保持该软件和MCP服务运行。读取使用实时数据，发送仍需微信窗口可操作。首次连接可按分页补录历史，只记录与整理，不回复历史消息。</p>
    {form&&<><Field label="MCP接口地址"><Input value={form.endpoint} onChange={v=>setForm({...form,endpoint:v})} /></Field>
      <Field label="访问令牌" hint="已保存时显示星号；直接保存星号会保留原令牌"><Input type="password" value={form.token} onChange={v=>setForm({...form,token:v})}/></Field>
      <Field label="账号" hint="留空使用服务默认账号，连接后锁定该账号"><Input value={form.account} onChange={v=>setForm({...form,account:v})}/></Field>
      <button disabled={saving} onClick={save} className="border rounded px-4 py-2">{saving?'保存中…':'保存MCP连接'}</button></>}
    {message&&<p className="text-xs">{message}</p>}
  </div>
}
