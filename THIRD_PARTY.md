# 第三方组件与数据

仓库提供本项目代码、图标与工作流模板，不分发论文PDF、嵌入模型权重、浏览器配置或第三方技能包。

- 可选嵌入使用`prototype_v2/model.lock.json`固定的模型文件；安装器从对应模型仓库下载并核对摘要。请同时遵守模型仓库的许可证。
- 可选Python依赖包括NumPy、ONNX Runtime、tokenizers；桌面入口使用PyGObject/GTK，图标由系统SVG加载器渲染。
- 可选视频辅助使用edge-tts与imageio-ffmpeg/FFmpeg；讲稿会发给TTS服务。请自行确认服务可用性、使用条款和媒体发布权利。
- Obsidian、Zotero、Chrome/Chromium和Codex由各自项目或服务提供；本项目与其没有官方隶属关系。
- 公开来源元数据、摘要、图片、PDF仍受来源许可约束。本项目MIT许可证不授予再分发他人论文或图片的权利。

公共目录和接口会变化；失败时应明确降级或等待，而不是绕过访问限制。
