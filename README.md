# IndexTTS LongAudio Workbench

一个面向 IndexTTS-2.5 的本地长文本语音工作台：参考音频音色库、自然断句、逐段生成、暂停/继续、失败重试、WAV/MP3 合并和浏览器可视化任务队列集中在一个界面中。

本项目只发布工作台代码，不发布 IndexTTS 源码、模型权重、参考录音、用户文稿或生成音频。工作台代码采用 AGPL-3.0；上游实现与模型请阅读 [IndexTTS 官方仓库](https://github.com/index-tts/index-tts) 和 [IndexTTS-2.5 模型卡](https://huggingface.co/IndexTeam/IndexTTS-2.5)。模型使用独立的 Bilibili Model Use License，发布或商用前请阅读上游许可证。

## 功能

- 粘贴或导入 `.txt/.md/.markdown`，保留中文段落与标点并预览切分结果。
- 通过参考 WAV/MP3/FLAC 音频克隆音色；内置 11 个上游官方示例，可逐个试听，也可上传和保存个人音色。
- 语言、段间停顿、外层字符分段、IndexTTS token 上限、语速倍率、情绪参考/情绪文本和采样参数可调。
- 每段独立落盘，任务支持开始、暂停、继续、取消、失败片段重试，完成后合并 WAV 与 MP3。
- 服务端只监听 `127.0.0.1`，通过 SSH 隧道从本机浏览器访问。

## 通用部署

先按上游文档准备可运行的 IndexTTS-2.5 Python 环境、源码、`checkpoints/` 和 FFmpeg，再安装本项目的 Web 依赖：

```bash
git clone https://github.com/ximizhou/IndexTTS-LongAudio-Workbench.git
cd IndexTTS-LongAudio-Workbench
python -m pip install -r requirements/server.txt
```

`scripts/start.sh` 需要 NVIDIA GPU 和 `nvidia-smi`。非 12 号机通过环境变量覆盖默认路径：

```bash
INDEXTTS_PYTHON=/path/to/env/bin/python \
INDEXTTS_SOURCE_DIR=/path/to/index-tts \
INDEXTTS_MODEL_DIR=/path/to/index-tts/checkpoints \
INDEXTTS_REFERENCE_DIR=/path/to/index-tts/examples \
INDEXTTS_WORKBENCH_FFMPEG=/path/to/ffmpeg \
PORT=8082 ./scripts/start.sh
```

可选项：`MIN_FREE_MIB` 设置启动所需最低空闲显存（默认 8192 MiB）；`INDEXTTS_QWEN_EMO=0` 可关闭文本情绪模型。服务仍只绑定本机回环地址。

## 12 号服务器

部署目录：`/data1/ximizhou/indextts-workbench`；官方源码和模型目录：`/data1/ximizhou/indextts`；环境：`/data1/ximizhou/envs/conda/indextts`。

在 12 号机远程桌面中，直接点击 Dock 上的“IndexTTS 长音频工作台”，或按 `Super` 搜索同名应用；该入口会检查 GPU、启动服务并打开浏览器，不需要打开终端。

在服务器上启动（脚本会重新查询 GPU，选择空闲显存最多且至少有 8 GiB 的卡）：

```bash
cd /data1/ximizhou/indextts-workbench
./scripts/start.sh
curl -fsS http://127.0.0.1:8082/healthz
```

Windows PowerShell 建立隧道并打开页面：

```powershell
ssh -N -L 8082:127.0.0.1:8082 12
```

浏览器打开 `http://127.0.0.1:8082`。关闭服务：

```bash
cd /data1/ximizhou/indextts-workbench
./scripts/stop.sh
```

脚本不创建开机自启、不开放公网端口。模型首次启动前需已下载到 `/data1/ximizhou/indextts/checkpoints`，辅助模型会由官方工具放在 `checkpoints/hf_cache`。

## 本地检查

```bash
python -m compileall -q app core tests scripts
python -m pytest -q
node --check app/static/app.js
```

真实 GPU 试听和 7000 字长文验收应在 12 号机上执行，并在启动前再次检查 `nvidia-smi`。运行数据位于服务器的 `tasks/`、`voices/`、`logs/`、`run/`，均不提交到 Git。

2026-08-24 的真实验收结果：短试听成功；7,000 字任务完成 50/50 片段、零失败、文本完整性为真，最终 1,256.456 秒 WAV/MP3 均通过 FFmpeg 解码；断点恢复仅重做未完成片段。自动测试为 `19 passed`。

更多 HTTP 字段见 [`docs/index-tts-ui-contract.md`](docs/index-tts-ui-contract.md) 和 [`docs/api.md`](docs/api.md)。
