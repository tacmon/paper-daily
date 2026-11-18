# 工具集成

## Obsidian

在Obsidian中选择“打开已有文件夹”，打开此仓库目录。每篇笔记仍位于原论文子目录；`metadata.artifacts.notes`是当前版本的路径。`python3 paper_library.py rebuild`会在已存在`.obsidian/`时生成精读导航，手工修改过的导航会保留并提示冲突。

桌面按钮通过`obsidian://`URI打开笔记，不要求CLI。自动检查或使用相关AI技能时可按Obsidian自身说明启用CLI；本仓库不附带、不安装第三方社区插件。当前自动库检测实现面向Linux的标准Obsidian配置目录。

浏览器HTML是报告交付时的排版快照；Obsidian编辑Markdown不会即时重建HTML。个人批注与最终一句请保留，不要被日报重建覆盖。

## Zotero

需要本机Zotero Desktop及可用的本地API/Connector。先手动建立目标分类，例如“Paper Daily by AI”，获得实际分类key。公开仓库不预填作者的分类key或用户ID。

`scripts/import_zotero.py`先做预检，默认不写入；`--apply`才提交。它要求目标分类与当前Connector目标相符，核验标识、条目与真实PDF附件，不应向其他库误写。它不创建分类，也不保证兼容每个Zotero版本。

```bash
python3 scripts/import_zotero.py --help
python3 scripts/import_zotero.py --metadata /path/to/zotero-item.json \
  --pdf /path/to/paper.pdf --collection-key YOUR_COLLECTION_KEY --out /path/to/result.json
# 核对预检和目标后，添加 --apply。
```

`zotero-item.json`是Zotero条目字段，不是论文库的metadata.json。至少包括合法`itemType`、`title`、`url`和`creators`，其他字段按Zotero类型填写。例如：

```json
{"itemType":"journalArticle","title":"Example title","url":"https://example.org/paper","creators":[{"creatorType":"author","firstName":"Example","lastName":"Author"}]}
```

本地Zotero请求不会通过外部代理。不要把登录凭据写入仓库。

## PPT、配音与视频

PPT创作和逐页渲染依赖使用者的AI助手及工具，例如其演示文稿技能。仓库没有通用的“一键写PPT”程序。

`scripts/render_lesson.py`是可选合成辅助：输入已完成的`slides.json`和`rendered/slide-1.png`等逐页画面，调用Edge TTS生成音频与时间戳，使用FFmpeg合成视频和字幕。`slides.json`格式为：

```json
[{"title":"科学问题","narration":"这里是适合口头讲述的完整讲稿。"}]
```

在项目内隔离安装可选依赖，勿改系统Python环境：

```bash
python3 -m venv .runtime/media-venv
.runtime/media-venv/bin/pip install edge-tts imageio-ffmpeg
mkdir -p .runtime/media
.runtime/media-venv/bin/python scripts/render_lesson.py /path/to/paper --runtime .runtime/media
```

`--runtime`是额外Python模块查找目录；使用虚拟环境时可指向空目录。FFmpeg字幕滤镜、中文字体和TTS服务需要实际可用；脚本遇到失败会保留状态并退出。默认音色/语速只是起点，不代表自然讲解质量已经验收。边界：此辅助不等于完整复现、科学事实核验或PPT质量检查。

## 每日一句

工作台`/reading-log`的“直接保存原话到表格”可将用户自己的总结直接写入`reading_log.md`，无需AI。也可以选择“先请AI润色”，保留原话并创建润色请求；“打开Codex”预填任务并需用户发送，也提供可复制的提示。AI通过`reading_journal.py suggest`提交建议；用户在页面对照后确认，才写入最终表格。其他助手也可按复制的提示执行同一文件操作。该功能不会后台发送LLM请求。

直接保存和润色确认共用行版本冲突保护、历史备份与用户原话记录。详细操作和AI接手规则见[JOURNAL.md](../JOURNAL.md)。
