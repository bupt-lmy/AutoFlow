# 影视语义剪辑默认示例

这个示例使用 FFmpeg 在本地生成一段 12 秒的合成视频，然后运行仓库自带的
`semantic-video-edit` 工作流，从静态开场和两段动态画面中剪出约 4 秒的高运动片段。
样片完全由测试图形生成，不包含第三方版权素材，也不会被提交到 Git。

## 最快运行

在仓库根目录完成 Python 安装后执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
./examples/semantic-video-edit/run-demo.sh
```

最低要求是 Python 3.11+、`ffmpeg` 和 `ffprobe`。没有 MiniMax Key 时，工作流会使用
本地运动、画面变化和音频特征完成确定性选片；设置 `MINIMAX_API_KEY` 后会额外启用
MiniMax-M3 多帧语义评分。没有 `whisper-cli` 或 Whisper 模型时，对白分析自动跳过，
并使用选片原因生成旁路 SRT，不会阻塞视频交付。

只生成样片、不执行工作流：

```bash
./examples/semantic-video-edit/run-demo.sh --generate-only
```

输出位置：

- 合成输入：`workspace/demo/semantic-video-demo-source.mp4`
- 中间结果：`workspace/media/`
- Demo 成片：`workspace/media/highlights/`（开启硬字幕时为 `workspace/media/final/`）

## 在 Web 平台查看

```bash
# 终端 1
.venv/bin/python -m uvicorn axonflow.api.app:app --host 127.0.0.1 --port 8000

# 终端 2
cd frontend && npm install && npm run dev -- --host 127.0.0.1
```

打开 `http://127.0.0.1:5173/workflows/semantic-video-edit`。工作流 YAML 位于
`config/workflows/semantic-video-edit.yaml`，会随源码自动显示在默认工作流列表中。
