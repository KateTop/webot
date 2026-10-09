# WeChatDataAnalysis MCP读取版

需要微信和WeChatDataAnalysis的本机MCP服务运行。地址默认http://127.0.0.1:13818/mcp。身份页选择WeChatDataAnalysis MCP，发送仍使用原有Windows自动操作；MCP+pywechat使用原实验性发送器。锁屏发送限制仍存在。

接入通过JSON-RPC 2.0进行initialize、notifications/initialized、分页tools/list和tools/call，优先structuredContent，兼容JSON/SSE。协议版本使用服务协商结果。本机地址、禁止代理/重定向，令牌不发往其他主机。

连接存于data/mcp_connection.json，界面以星号显示令牌，星号保存保留现有令牌。不要提交或分享此运行配置。系统.env中WECHAT_BACKEND选择mcp或mcp_pywechat。

读取只接受source=realtime且无回退。工具不可用或连接失败会报错及重连，不使用旧快照触发回复。每请求至多50条、应用端分页补录历史；初次历史较多时启动可能较慢。补录不会发送历史回复，整理可能产生正常AI费用。

账号连接后锁定。使用serverIdStr保存精确ID，艾特按atUsernames、引用按quoteUsername与本账号稳定ID识别。发送确认仍需在目标群新消息里观察到本账号正文；调用RPA成功不算确认。

当前MCP没有发送接口，也没有完整实时群成员名册工具。成员别名使用现有本地缓存和本次已观察到的发言人，不能承诺所有未发过言成员的昵称可解析。媒体留作URL/标记，不内联二进制；语音优先服务返回的已有转写。

本版已做小批量实时读取和合成消息转换；未向真实群发测试内容。初始soul不覆盖既有记忆，配置和提示词沿用。
