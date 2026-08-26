# HTTP API

服务默认运行在 `127.0.0.1:8082`。接口不会返回参考音频的绝对路径；路径、原文和生成音频只保存在服务器运行目录。

## 路由

| 方法 | 路由 | 作用 |
| --- | --- | --- |
| `GET` | `/healthz`, `/api/health` | 健康检查，不加载模型 |
| `POST` | `/api/preview` | 规范化并预览长文切分 |
| `GET` | `/api/voices` | 返回内置参考音色和用户保存音色 |
| `POST` | `/api/voices/preview` | 用参考音频生成短试听 WAV |
| `POST` | `/api/voices/upload` | 上传并保存用户参考音频 |
| `DELETE` | `/api/voices/{id}` | 删除用户保存的参考音色，内置音色不可删除 |
| `POST` | `/api/jobs` | 创建任务 |
| `POST` | `/api/jobs/{id}/start` | 开始任务 |
| `POST` | `/api/jobs/{id}/pause` | 在当前片段完成后暂停 |
| `POST` | `/api/jobs/{id}/resume` | 继续未完成片段 |
| `POST` | `/api/jobs/{id}/cancel` | 取消任务 |
| `POST` | `/api/jobs/{id}/retry` | 重新排队失败片段 |
| `GET` | `/api/jobs/{id}/segments/{index}/audio` | 获取片段 WAV |
| `GET` | `/api/jobs/{id}/download/{wav,mp3,srt}` | 获取最终合并音频或 SRT 字幕 |

## 创建任务示例

```bash
curl -fsS http://127.0.0.1:8082/api/jobs \
  -H 'content-type: application/json' \
  -d '{"text":"欢迎使用 IndexTTS。","voice_id":"index-example-01","lang":"zh","max_chars":140,"pause_ms":260,"max_text_tokens_per_segment":120,"duration_factor":1.0,"emotion_random":false}'
```

主要字段：`voice_id`、`lang`（`zh/en/ja/es/ar`）、`max_chars`（80–240）、`pause_ms`、`duration_factor`（0.5–2）、`emo_alpha`（0–1）、`emotion_text`、`use_emo_text`、`emotion_random`、`max_text_tokens_per_segment`（60–220）、`temperature`、`top_p`、`top_k`、`max_mel_tokens`、`repetition_penalty`、`seed`。服务端会再次校验所有范围。

## 数据边界

`reference_audio`、`emotion_reference_audio` 等绝对路径只用于服务器内部推理，不会出现在 API 的公开 `settings` 中。上传音频必须是 WAV/MP3/FLAC/OGG/M4A/AAC 且不超过 50 MiB；任务文本最多 2,000,000 字符或上传文件 20 MiB。
