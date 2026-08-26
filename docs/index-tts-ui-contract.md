# IndexTTS LongAudio Workbench 前端契约

本文件描述 `app/static/index.html`、`app/static/styles.css` 和 `app/static/app.js` 之间的浏览器契约。页面只面向本机 FastAPI 服务，默认通过 `127.0.0.1` 和 SSH 隧道访问；参考音频、文稿和生成结果留在服务端运行目录，不进入 Git。

## 页面能力

- **创作**：粘贴或导入 `.txt/.md/.markdown`，显示字符数；调用切分预览接口，展示规范化文本和有序片段；提交长文任务。
- **参考音色**：通过 `/api/voices` 读取内置/已保存参考音色；上传 WAV、MP3 或 FLAC（前端限制 50 MiB），保存名称和备注；试听并选择音色。
- **任务**：轮询 `/api/jobs`，查看每个片段的状态、重试次数和进度；片段成功后可单独试听/下载；任务支持开始、暂停、继续、取消、失败重试，并在任务完成后下载 WAV、MP3 和适用于剪映/必剪的 SRT 字幕。

## 创建/预览参数

浏览器请求同时携带当前后端兼容字段和 IndexTTS 统一字段。旧字段保证现有 FastAPI 适配器可用，新字段供后续 API 迁移直接使用。

| UI 含义 | 当前兼容字段 | 统一字段 | 默认/范围 |
| --- | --- | --- | --- |
| 文稿 | `text` | `text` | 1–2,000,000 字符 |
| 语言 | `lang` | `language` | `zh/en/ja/es/ar` |
| 情绪方式 | `use_emo_text` | `emotion_method` | `reference/prompt` |
| 语速 | `duration_factor` | `speech_rate` | `0.5–2.0` |
| 情绪权重 | `emo_alpha` | `emotion_weight` | `0–1` |
| 每段最大字符 | `max_chars` | `max_chars` | `80–240`（实际上限由服务端校验） |
| 每段最大模型 token | `max_text_tokens_per_segment` | `max_segment_tokens` | `60–220`（以服务端模型上限为准） |
| 段间停顿 | `pause_ms` | `pause_ms` | `0–10,000 ms` |
| 参考音色 | `voice_id` | `reference_voice_id/reference_audio_id` | 已保存音色 ID |
| 采样 | `seed/temperature/top_p/top_k` | 同名字段及 `sampling` | 见服务端校验 |

前端还会传 `emotion_text`、`emotion_random`、`max_mel_tokens`、`repetition_penalty`、`num_beams` 和 `do_sample`。不支持这些字段的服务端应忽略未知字段，而不是改变分段文本。

## HTTP 路由

页面使用以下路由；`/api/reference-voices*` 是预留的统一命名，当前页面默认使用已经落地的 `/api/voices*`。

| 方法 | 路由 | 请求/响应约定 |
| --- | --- | --- |
| `POST` | `/api/preview` | JSON `{text, ...settings}`；返回 `normalized_text`、`segments[]`、`integrity` |
| `POST` | `/api/jobs` | JSON `{text, ...settings}`；返回 `job_id`、`segments`、`summary` |
| `POST` | `/api/jobs/upload` | multipart `file` + 同一组设置；文件仅用于创建任务 |
| `GET` | `/api/voices` | 返回 `{presets: [], saved: []}`；每项至少有 `id/name/description/kind` |
| `POST` | `/api/voices/upload` | multipart `file/name/description`；返回新音色对象 |
| `POST` | `/api/voices/preview` | JSON `{text, voice_id, ...settings}`；返回 `audio_url` |
| `DELETE` | `/api/voices/{voice_id}` | 删除用户保存的参考音色；已生成任务不受影响 |
| `GET` | `/api/jobs`、`/api/jobs/{job_id}` | 返回任务摘要/manifest；片段状态可为 `pending/running/success/failed/cancelled` |
| `POST` | `/api/jobs/{job_id}/{start,pause,resume,cancel,retry}` | 返回更新后的任务 manifest |
| `GET` | `/api/jobs/{job_id}/segments/{index}/audio` | 返回单片段 WAV |
| `GET` | `/api/jobs/{job_id}/download/{wav,mp3,srt}` | 返回最终合并音频或 UTF-8 SRT 字幕；SRT 每条末尾标点已移除 |

统一接口可增加下列等价路由：`/api/reference-voices`、`/api/reference-voices/upload`、`/api/reference-voices/preview` 和 `/api/reference-voices/{id}`。前端迁移时只需替换路由，不改变 DOM 或参数表。

## 安全与数据边界

参考音频上传必须校验扩展名、大小和服务端实际 MIME；路径只能落在运行目录内。服务不得把原始文稿、音频、模型权重、凭据或 `.env` 内容写入源码仓库。生产/共享服务器默认只监听 `127.0.0.1`，通过 SSH 本地转发访问，不创建公网分享链接。
