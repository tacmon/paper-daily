# 归档格式

论文按稳定ID保存，日期记录只指向论文，不复制媒体：

```text
papers/<stable-id>/
  metadata.json        标识、原题/中文题、作者、状态、产物路径、核验证据
  paper.pdf            完整原文
  citation.bib         引用
  reading.md           精读、证据定位、理解问题
  background.md        问题沿革、作者机构、前驱与后续
  notes.md / notes.html
  slides.pptx / script.md / video.mp4 / subtitles.srt
  revisions/           保留的修订版本
days/YYYY-MM-DD/paper.json
reading_log.md         AI候选句和用户最终句分列
indexes/               可重建的作者、引用与日期索引
```

```bash
python3 paper_library.py register --arxiv 1705.06366 --pmlr v80/florensa18a \
  --title 'Automatic Goal Generation for Reinforcement Learning Agents' \
  --author 'Carlos Florensa' --author 'David Held' --author 'Xinyang Geng' \
  --author 'Pieter Abbeel' --year 2018
python3 paper_library.py rebuild
python3 paper_library.py validate
python3 paper_library.py validate --require-ready
```

这是公开论文的登记示例，执行前仍应核验版本与官方来源；登记不会下载全文或声称已读。新建状态为`partial`。全部产物存在、PDF头与PPTX结构正确、核验布尔项为真且视频时长600–900秒，才满足结构上的`ready`条件。桌面通知还要求非空背景材料。

核验字段：`full_text_read`、`slides_rendered`、`video_audio_checked`、`video_subtitles_checked`、`video_duration_checked`和`video_duration_seconds`。这些字段由执行者根据实际检查填写；校验器不会自动听视频或核实论文主张。

补充`title_zh`与`title_zh_is_translation`供中文展示。`artifacts`中的路径相对于论文目录，修订时指向新版本。不要用软链接逃出论文目录。

引用关系只有`relation=cites`、`verified=true`、有`evidence.source`和`evidence.locator`，且目标已归档时才创建相对软链接。共同作者与主题相似不等于引用；同名作者可能需要ORCID等额外区分。

JSON使用临时文件原子替换；跨多个JSON不是数据库事务。一次只运行一个归档写入者，中断后运行校验再恢复。索引可重建，个人笔记与论文不能靠索引恢复。
