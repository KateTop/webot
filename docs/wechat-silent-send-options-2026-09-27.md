# 微信 4.1.12.26 静默发送接口调研

调研日期：2026-09-27。目标是让 WeBot 在 Windows 锁屏后仍能按群发送回复。下面的“支持版本”只表示仓库公开内容中的声明或实现；未在当前微信账号上验证真实发送。

| 项目 | 公开实现与版本 | 对当前 WeBot 的意义 |
| --- | --- | --- |
| [mrsanshui/WeChatApi](https://github.com/mrsanshui/WeChatApi) | 公开了 3.9.10.19 的 C++ `SendTextMsg` 示例；唯一 4.x 示例针对 4.1.2.16 的自动更新开关。仓库没有公开 4.1.12.26 的发送代码或完整 HTTP 服务实现，也未发现许可证文件。 | 旧代码证明进程内发送的技术路径，但硬编码的函数偏移和结构不能直接用于当前微信。 |
| [lich0821/WeChatFerry](https://github.com/lich0821/WeChatFerry) | MIT；提供注入式消息接收与发送，README 的适配记录到 3.9.12.51；仓库已归档。 | 接口形态适合 WeBot，但不支持当前 4.1.12.26。 |
| [ttttupup/wxhelper](https://github.com/ttttupup/wxhelper) | MIT；DLL 注入后在本机开放 HTTP 端口。README 列出的版本最高为 3.9.5.81。 | 可参考 HTTP 适配层，不能直接注入当前 4.1.12.26。 |
| [miloira/wxhook](https://github.com/miloira/wxhook) | MIT；公开 Python 包中的 `Bot.version` 固定为 3.9.5.81，附带编译好的 `wxhook.dll`。仓库简介虽然列出 4.1.12.26，但 README 将 4.x 功能指向另一个通过 QQ 获取的 `pywechat`，没有在本仓库提供该版本的 DLL 源码。 | 不能把仓库简介视为公开的 4.1.12.26 发送实现。 |
| [kanadeblisst00/pywxrobot4](https://github.com/kanadeblisst00/pywxrobot4) | README 写明支持 4.1.1.19；公开仓库只有文档，二次开发 HTTP 接口另行收费。 | 版本不匹配，也不能作为可修改的开源发送模块。 |
| [scottfly189/WeChatAuto.SDK](https://github.com/scottfly189/WeChatAuto.SDK) | MIT；支持新版微信的 .NET/UI 自动化，README 提到 4.1.11.xx。 | 可替代部分窗口操作，但仍依赖 UI，不能据此解决 Windows 锁屏发送。 |

## WeChatApi 源码能否移植

[3.9.10.19 的发送示例](https://github.com/mrsanshui/WeChatApi/blob/master/005-PC%E5%BE%AE%E4%BF%A1_%E6%BA%90%E7%A0%81_x64_3.9.10.19/SendTextMsg/dllmain.cpp) 在注入 DLL 后按固定偏移调用 `WeChatWin.dll` 内部函数，收件人和测试文字也写死在源码里。[4.1.2.16 的示例](https://github.com/mrsanshui/WeChatApi/blob/master/006-PC%E5%BE%AE%E4%BF%A1_%E6%BA%90%E7%A0%81_x64_4.1.2.16/AutoUpdateSwitch/dllmain.cpp) 操作的是 `Weixin.dll` 自动更新设置。两者没有可复用的现成 4.1.12.26 发送调用地址或参数结构。

可以独立开发适配当前版本的发送模块，但需要先定位 4.1.12.26 的发送函数及数据结构、编写注入与本机接口、处理登录状态和错误回执，并用文件传输助手验证后再接入群聊。原仓库没有许可证文件，若要复制其源码并随公开的 WeBot 仓库重新分发，还需要先厘清授权。

WeBot 已有 `AbstractWeChatBackend.send_text(chat_id, content)` 接口；Windows `WcdbBackend` 当前用数据库接收、`WeChatWindowController` 通过前台窗口发送。若有经过验证的本机发送 API，最小改动是在发送层加入可配置的适配器，保留当前接收、群隔离和去重逻辑。适配器必须使用当前群的 `chat_id`，并区分“明确发送成功”“明确失败”“状态未知”，避免在状态未知时自动重发。

## 当前结论

在本次检查的公开仓库中，没有找到已公开源码、明确支持微信 4.1.12.26、并已验证可用于锁屏发送的现成接口。WeChatApi 的旧版本示例可用于研究，不能直接编译后塞入当前 WeBot。下一步应先取得一个兼容当前版本且实际可发送的本机接口，或单独完成该版本的底层适配，再做 WeBot 集成。
