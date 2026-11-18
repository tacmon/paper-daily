# 安装与日常使用

## 从空目录开始

按README克隆后运行`python3 scripts/init_library.py`。它不会联网、下载模型、创建定时任务或覆盖已有配置。工作台可以先用空库打开；第一次完整精读完成后，中控才有论文卡片。

本地文件：

| 文件 | 修改内容 |
| --- | --- |
| `reading_profile.md` | 研究兴趣、已有基础、知识缺口与阅读偏好 |
| `config.json` | 工作流语言、时间等约定；它不是调度器 |
| `pipeline_prompt.md` | 给AI助手的每日任务提示 |
| `prototype_v2/sources.json` | 采集来源、超时与可选代理 |
| `prototype_v2/research_queries.json` | 最多4个检索问题 |
| `prototype_v2/discovery.json` | 增量巡查、会议/期刊目录和经典种子 |

默认主题偏向强化学习。换领域时还应修改`prototype_v2/radar.py`的`TOPICS`，避免旧的关键词加权影响候选展示。来源配置可增删；未实现的来源类型需要新的解析器，不能仅填一个网址。

工作台监听`127.0.0.1:8766`，不对公网开放。若端口已经被使用，请停止自己启动的旧实例，或一致修改`server.py`的`PORT`及页面中的相关链接；当前版本还没有统一的动态端口配置。

## 每天06:00

每日生成与桌面提醒是两个环节：

1. **AI助手任务**：在支持定时任务的宿主里选择本项目、设置每天06:00及正确时区，把本地`pipeline_prompt.md`作为任务依据。先手动跑通；不要把本仓库配置文件当作已经启用日程。
2. **本地提醒器**：登录桌面后观察`days/`和`papers/`，看到经过验收的完整报告才弹窗。它不启动AI，不补跑错过的生成任务。

使用Codex时，以宿主当前支持的[官方定时任务说明](https://learn.chatgpt.com/docs/automations?surface=app)为准；运行权限、机器状态及项目路径要在实际环境中核验。本项目不修改宿主数据库、不附带后台LLM runner，也不承诺离线关机时生成报告。

当前归档日期固定采用北京时间：`paper_library.py`中的`TIMEZONE`是实际代码设置。若需要其他时区，不能只编辑`config.json`；还需相应调整代码、调度和测试。

## 中控与自动提醒

安装入口后，在系统应用列表搜索“论文日报”。手动`--open`显示最近一期已就绪报告，含已经关闭过的提醒；重复启动只唤起现有手动窗口，关闭不会写入自动提醒记录。

```bash
/usr/bin/python3 scripts/paper_notify.py          # 启动/确认自动观察器
/usr/bin/python3 scripts/paper_notify.py --check # 列出尚未确认的完成记录
/usr/bin/python3 scripts/paper_notify.py --open  # 手动中控
/usr/bin/python3 scripts/paper_notify.py --stop  # 停止本次观察器
```

安装时加`--autostart`才登记登录自启动。永久关闭时删除当前用户的`~/.config/autostart/paper-daily-ai.desktop`（使用XDG_CONFIG_HOME时以该目录为准）。已存在另一个目录的同名安装时，安装器会拒绝覆盖；请先确认要使用哪一套。

自动窗口只有明确关闭才确认，打开视频或笔记不会让它消失。GNOME锁屏状态不明确时会保留待提醒；其他桌面尤其Wayland需要单独验证。手动窗口不要求置顶，便于切换阅读应用。

## 备份与迁移

Git忽略个人资料和媒体，因此仅推送Fork不能备份论文库。迁移时另行备份`papers/`、`days/`、`reading_log.md`、个人配置、反馈与笔记。运行缓存可以重建；模型权重按需要重新下载。移动项目后重新安装桌面入口，并更新宿主任务的项目路径。
