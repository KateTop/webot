# webot

[更新日志](CHANGELOG.md)

> 微信群的 AI 助手 —— 帮你总结聊天、回答问题，并按群整理长期记忆。

<p align="center">
  <img src="https://img.shields.io/badge/platform-Windows%2010%2F11%20%7C%20macOS-blue?style=flat-square&logo=windows" alt="Platform" />
  <img src="https://img.shields.io/badge/python-3.13%2B-green?style=flat-square&logo=python" alt="Python" />
  <img src="https://img.shields.io/badge/AI-Claude%20%7C%20DeepSeek-purple?style=flat-square" alt="AI Backend" />
  <img src="https://img.shields.io/badge/license-MIT-yellow?style=flat-square" alt="License" />
</p>

> ## 使用声明
> 
> 本项目仅供学习交流使用。严禁用于发送垃圾信息、骚扰他人、诈骗钓鱼等违法违规行为。使用者自行承担一切后果与风险。

---

## 它是什么

把 webot 加入你的微信群，它会：

- **总结聊天记录**：说一句「总结一下」，立刻告诉你在忙的时候群里聊了什么。
- **回答你的问题**：@机器人 问任何问题，它会结合群聊上下文回答你。
- **偶尔自己冒泡**：群聊热闹的时候，它也会插话接梗，像个普通群友。

所有操作都在一个网页控制台里完成，双击 EXE 就能用。

---

## 能做什么

### 1. 聊天总结

忙了几个小时没看群？直接说一句「总结一下」，机器人把刚才的聊天整理好发给你：谁说了什么、讨论了什么话题。

**怎么用**

群里直接说 `总结一下` 就行。也可以说 `前面说了什么`、`发生了什么`、`summarize`。触发词可以在控制台自定义。

控制台里可以调整：总结往前看多久（默认 8 小时）、用哪些词触发。

### 2. 对话问答

@机器人 然后问问题，它会结合最近的群聊内容来回答，而不是凭空瞎猜。

**怎么用**

- `@机器人 DeepSeek 和 Claude 哪个好？`
- `@机器人 刚才他们说的那个餐厅地址是什么？`

### 3. 群聊记忆与昵称

控制台的「记忆」页面按群展示可编辑的 `soul.md`。你可以预览聊天库中尚未整理的时间段，按顺序手动凝固到记忆，并编辑自动与手动整理共用的指令。页面也提供群友昵称编辑。

**怎么用**

打开控制台 → 记忆 → 选择群聊 → 群友昵称，在对应成员旁填上容易识别的名字。

也可以在群里直接发送命令（仅管理员）：`@机器人 改名 wxid_xxx = 张三`

### 4. 主动发言

不用 @它，群聊热闹到一定程度时，机器人会自己插话。

**怎么用**

在控制台的「功能开关 → 主动发言」里打开。可以调整它在多热闹的群里才开口（安静时闭嘴，热闹时活跃）。

### 5. 欢迎新人

有人刚进群，机器人会自动发一条欢迎消息。

**怎么用**

在控制台的「功能开关 → 欢迎新人」里打开。可以写多条欢迎词模板，不同的群用不同的模板。

---

## 控制台一览

打开 EXE 后会自动弹出网页控制台，左侧菜单依次是：

- **运行状态**：看机器人在不在线、处理了多少消息
- **系统配置**：设置 AI 后端和 Key、机器人名字、数据目录、测试提示词
- **功能开关**：各项功能的开关和参数调整
- **记忆**：编辑每群的 `soul.md`、整理指令、未整理对话与群友昵称
- **运行日志**：查看机器人运行记录

所有配置改完后点「保存配置」，重启机器人就生效。

---

## 安装

### Windows（推荐）

1. 从 [Releases](https://github.com/cancelGuMu/webot/releases) 下载 `webot-setup.exe`，双击安装
2. 或者下载 `webot.exe`，免安装直接双击运行
3. 首次打开会弹出配置向导，跟着走完就行

前提：电脑上已登录微信，想让机器人进的群需要先添加到通讯录（群聊右上角 `···` → 开启「添加到通讯录」）。

### macOS（实验）

```bash
git clone https://github.com/cancelGuMu/webot.git
cd webot
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-macos.txt
cd ui && npm install && npm run build && cd ..
python desktop_mac.py
```

需要在系统设置中给终端授权「辅助功能」权限。

### 从源码运行（Windows）

```bash
git clone https://github.com/cancelGuMu/webot.git
cd webot
pip install -r requirements.txt
cd ui && npm install && npm run build && cd ..
python desktop.py
```

---

## 配置

所有设置都在控制台里直接改，不需要手动编辑文件。保存后重启机器人即可生效。

必须填的只有一项：在「系统配置 → AI 后端配置」里填入 API Key（DeepSeek 或 Claude 二选一）。

---

## 常见问题

**需要装什么？**
下载 EXE 双击就行，什么都不用装。

**会被封号吗？**
目前没有已知案例，但请自行评估风险。

**支持哪些 AI？费用多少？**
DeepSeek（推荐，极低价格）和 Claude。普通群一天用下来不到一毛钱。

**微信能最小化吗？**
读消息不受影响，发消息时微信窗口需要可见。

**支持多群吗？**
支持，默认监控所有群，也可以指定。

---

## 许可证

MIT © [cancelGuMu](https://github.com/cancelGuMu)

---

<p align="center">
  <sub>Made with ❤️ by <a href="https://github.com/cancelGuMu">孤舟99</a></sub>
</p>
