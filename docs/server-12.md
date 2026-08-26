# 12 号服务器运行手册

最后核验：2026-08-27。工作台目录为 `/data1/ximizhou/indextts-workbench`，官方 IndexTTS 源码为 `/data1/ximizhou/indextts`，模型为 `/data1/ximizhou/indextts/checkpoints`，环境为 `/data1/ximizhou/envs/conda/indextts`。服务默认只监听 `127.0.0.1:8082`。

## 启动

远程桌面提供两个入口：点击 Dock 上的“IndexTTS 长音频工作台”使用自定义长文界面；按 `Super` 搜索“IndexTTS 官方 WebUI”使用官方 Gradio 界面。自定义入口位于 `/home/ximizhou/.local/share/applications/IndexTTS-Workbench.desktop`，官方入口位于 `/home/ximizhou/.local/share/applications/IndexTTS-Official-WebUI.desktop`。两者共用模型，启动器会阻止同时运行。

```bash
cd /data1/ximizhou/indextts-workbench
nvidia-smi --query-gpu=index,name,memory.used,memory.free --format=csv,noheader
./scripts/start.sh
curl -fsS http://127.0.0.1:8082/healthz
tail -n 50 logs/server.log
```

`start.sh` 会读取实时空闲显存：GPU 3 达到最低阈值时固定优先；GPU 3 不存在或低于阈值时，才从其他卡中选择空闲显存最多的一张，然后设置 `CUDA_VISIBLE_DEVICES`。不要启动多个 Uvicorn worker。IndexTTS 模型加载发生在第一次试听/生成时，服务健康检查本身不会占用完整模型显存。

常用启动变量：`PORT`（默认 8082）、`MIN_FREE_MIB`（默认 8192）、`PREFERRED_GPU_ID`（默认 3）、`INDEXTTS_QWEN_EMO`（默认 1），以及用于覆盖安装位置的 `INDEXTTS_PYTHON`、`INDEXTTS_SOURCE_DIR`、`INDEXTTS_MODEL_DIR`、`INDEXTTS_REFERENCE_DIR`、`INDEXTTS_WORKBENCH_FFMPEG`。`INDEXTTS_WORKBENCH_TASKS` 和 `INDEXTTS_WORKBENCH_VOICES` 可改变私有运行目录；验收故障注入变量 `INDEXTTS_WORKBENCH_FAIL_INDEX_ONCE` 不应出现在日常启动命令中。

本机访问：

```powershell
ssh -N -L 8082:127.0.0.1:8082 12
```

浏览器打开 `http://127.0.0.1:8082`。不允许绑定 `0.0.0.0` 或创建公网分享链接。

## 官方 WebUI

官方入口运行上游 `/data1/ximizhou/indextts/webui.py`，只监听 `127.0.0.1:7860`：

```bash
cd /data1/ximizhou/indextts-workbench
./scripts/start-official-webui.sh
curl -fsS http://127.0.0.1:7860/ | head
./scripts/stop-official-webui.sh
```

本机访问：`ssh -N -L 7860:127.0.0.1:7860 12`，然后打开 `http://127.0.0.1:7860`。启动器默认传入 `--fp16`（IndexTTS-2.5 在当前 GPU 上使用 BF16）；设置 `INDEXTTS_OFFICIAL_FP16=0` 可关闭。官方 WebUI 与自定义工作台不能同时运行；对应日志为 `logs/official-webui.log`，PID 文件为 `run/official-webui.pid`。

## 停止

```bash
cd /data1/ximizhou/indextts-workbench
./scripts/stop.sh
ss -ltnp 'sport = :8082'
```

停止脚本只接受 PID 文件中、工作目录和命令行都匹配本项目的 Uvicorn 进程，不使用无目标 `pkill`。

## 模型和参考音频

主模型必须包含 `checkpoints/config.yaml` 及权重。首次使用前，官方辅助模型下载器会准备 `checkpoints/hf_cache/`（w2v-bert、MaskGCT semantic codec、CAMPPlus、BigVGAN）。官方示例音频位于 `/data1/ximizhou/indextts/examples`，工作台展示其中 11 个 `voice_*.wav` 参考录音供逐个试听；这些是参考音频，不是虚构的模型内置 speaker seed。

下载过程中使用的代理只在下载进程环境变量中设置；完成后应关闭本机到服务器的 SSH 反向代理。模型权重、参考音频、用户文本、任务 manifest 和生成结果不进入 GitHub。

## 长文验收

先用界面的“预览切分”确认片段数量，再用短句试听。长文建议 `max_chars=140`、`max_text_tokens_per_segment=120`、关闭“情绪采样”，段间停顿 200–400 ms。IndexTTS 官方本身也会在 token 超限时分段；工作台外层切分负责可见进度、重试和最终合并。

可用 API 检查任务：

```bash
curl -fsS http://127.0.0.1:8082/api/voices
curl -fsS http://127.0.0.1:8082/api/jobs
```

2026-08-24 已完成真实验收：短试听生成 5.06 秒 WAV；7,000 字任务 `20260824-155344-bcc3e0d8` 完成 50/50 片段、零失败、`text_integrity=true`，最终音频 1,256.456 秒，WAV 55,409,734 bytes、MP3 25,130,884 bytes，均通过 FFmpeg 解码。恢复测试保留前两个成功片段，只重做索引 2-11，最终 12/12 成功且文本完整。2026-08-27 已验证已有完成任务可补生成 UTF-8-BOM SRT；236 条字幕的结束时间与 913.778 秒 WAV 相差 0.001 秒。字幕格式 v2 会移除每条末尾标点并在下载时升级旧任务。GPU 选择器的优先、回退分支及真实 GPU 3 启动均已核验；自动测试为 `23 passed`。

工作台不配置开机自启。交付时服务会停止并释放 GPU；下次通过应用入口或 `./scripts/start.sh` 启动时仍须以实时 `nvidia-smi` 结果选卡。

## 排查

- 页面打不开：确认 `curl .../healthz` 成功、`ss -ltn 'sport = :8082'` 有监听，再检查 SSH 隧道端口是否一致。
- 试听时报模型缺失：检查 `checkpoints/config.yaml`、权重和 `checkpoints/hf_cache`，查看 `logs/server.log`。
- 无可用 GPU：脚本要求至少 `MIN_FREE_MIB`（默认 8192 MiB）空闲显存；等待其他任务释放后重试。
- 参考音色不可用：检查 `/data1/ximizhou/indextts/examples/voice_01.wav` 等文件存在且为有效音频。
