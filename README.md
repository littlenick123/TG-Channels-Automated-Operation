# Telegram 多频道自动运营

这是一个运行在 Linux/VPS 上的 Telegram 频道自动运营工具。程序使用一个 Telethon 用户会话读取源频道，下载符合条件的视频媒体组，通过 FFmpeg 转码和截图，再将处理结果作为一个 Telegram 媒体组发布到目标频道。

项目支持配置多个独立频道组，例如：

```text
源频道 A → 目标频道 B
源频道 A → 目标频道 C
源频道 A → 目标频道 D
源频道 A → 目标频道 E
源频道 A → 目标频道 F
```

频道组严格按照 `config.toml` 中的书写顺序执行。程序始终串行运行：一个媒体组完成下载、转码、截图、上传、状态保存和临时文件清理后，才会处理下一个媒体组；当前频道组结束后，才会进入下一个频道组。

每个频道组使用独立的 SQLite 数据库，选择记录、发布记录、失败状态和每日完成数量互不影响。同一个源媒体可以分别被 B、C、D 等不同目标频道选中，但在同一个目标频道对应的数据库中，成功发布后不会再次使用。

## 目录

- [主要功能](#主要功能)
- [处理流程与输出格式](#处理流程与输出格式)
- [运行条件](#运行条件)
- [准备 Telegram 账号和报告机器人](#准备-telegram-账号和报告机器人)
- [本地源码运行（可选）](#本地源码运行可选)
- [.env 完整说明](#env-完整说明)
- [config.toml 完整说明](#configtoml-完整说明)
- [五个目标频道的完整配置示例](#五个目标频道的完整配置示例)
- [命令行完整说明](#命令行完整说明)
- [首次运行顺序](#首次运行顺序)
- [多频道串行、数据库和失败处理](#多频道串行数据库和失败处理)
- [Docker 生产部署](#docker-生产部署)
- [更新源码和升级](#更新源码和升级)
- [日志与故障排查](#日志与故障排查)
- [安全建议](#安全建议)
- [开发与测试](#开发与测试)

## 主要功能

- 使用 Telethon 用户账号读取公开或私密源频道。
- 首次扫描完整历史，以后根据各数据库的检查点增量扫描。
- 仅选择具有 `grouped_id`、恰好包含一个视频且视频短边达到指定分辨率的媒体组。
- 从源媒体组中只保留视频，不保留源媒体组原有图片。
- 使用多路交错分片下载单个视频，支持 `.part` 文件断点续传。
- 使用 FFmpeg 将视频转为 H.264/AAC、720P、`yuv420p` 并启用 `faststart`。
- 为视频单独生成 10% 时间点的封面。
- 在视频 15%、50%、85% 时间点生成三张内容截图。
- 按标签过滤规则和简介规则生成新文案。
- 使用一次 Telethon 相册发送，将视频和三张截图作为同一个媒体组发布。
- 文案只附在媒体组第一个视频上，视频启用流式播放。
- 每个频道组分别配置每日成功数量，并按唯一组名自动创建独立数据库。
- 频道被封、不可访问、无发帖权限或数据库异常时跳过当前频道组并继续后续组。
- 使用独立 Telegram Bot API 机器人向私人会话发送即时告警和最终汇总。
- 使用全局进程锁防止两个任务同时运行。

## 处理流程与输出格式

每个被选中的源媒体组按以下顺序处理：

1. 检查源消息和本地磁盘空间。
2. 将源视频下载为 `source_video.mp4`，网络中断时保留 `.part` 文件。
3. 使用 FFprobe 检查视频时长、分辨率、旋转信息和音频流。
4. 使用 FFmpeg 转码为 `video.mp4`。
5. 从转码成品的 10% 时间点生成 `video_thumb.jpg`，仅作为 Telegram 视频封面。
6. 从转码成品的 15%、50%、85% 时间点生成三张内容截图。
7. 生成过滤后的标签和简介文案。
8. 上传视频、封面和图片，并通过一次相册发送请求发布。
9. 将发布结果写入当前频道组自己的 SQLite 数据库。
10. 删除当前媒体组的本地临时文件，然后才处理下一组。

目标频道中可见的媒体组固定为：

1. `video.mp4`
2. `frame_1.jpg`
3. `frame_2.jpg`
4. `frame_3.jpg`

四个媒体项共享一个 Telegram `grouped_id`，客户端会把它们显示成一个帖子。`video_thumb.jpg` 只作为视频封面上传，不会作为第五个媒体项显示。文案只附在第一个视频上，三张图片没有重复文案。

截图时间示例：

| 视频时长 | 视频封面 10% | frame_1 15% | frame_2 50% | frame_3 85% |
|---:|---:|---:|---:|---:|
| 60 秒 | 6 秒 | 9 秒 | 30 秒 | 51 秒 |
| 200 秒 | 20 秒 | 30 秒 | 100 秒 | 170 秒 |
| 600 秒 | 60 秒 | 90 秒 | 300 秒 | 510 秒 |

## 运行条件

推荐环境：

- Ubuntu 24.04 LTS。
- Docker Engine 和 Docker Compose plugin。
- 4 核 CPU、6 GiB 或更多内存。
- 足够容纳源视频、转码视频和临时文件的磁盘空间。
- 能稳定访问 Telegram MTProto 和 `api.telegram.org` 的网络。
- 一个已经加入所有源频道的 Telegram 用户账号。
- 该用户账号在所有目标频道中具有发帖权限。
- 从 [my.telegram.org](https://my.telegram.org/) 获取的 `api_id` 和 `api_hash`。
- 一个由 BotFather 创建的报告机器人。

生产镜像已经包含 Python 3.12、FFmpeg、FFprobe 和全部 Python 依赖，宿主机不需要另外安装这些组件。只有选择不使用 Docker、直接从源码运行时，才需要宿主机提供 Python 3.12 和 FFmpeg/FFprobe 5.1 或更高版本。

程序处理媒体前要求可用磁盘至少为：

```text
源视频文件大小 × 3 + disk_reserve_bytes
```

示例配置的 `disk_reserve_bytes` 为 1 GiB。如果源视频为 2 GiB，程序要求工作目录所在文件系统至少有约 7 GiB 可用空间。

## 准备 Telegram 账号和报告机器人

### 1. 获取 API ID 和 API Hash

1. 使用准备运行脚本的 Telegram 用户账号登录 [my.telegram.org](https://my.telegram.org/)。
2. 打开 `API development tools`。
3. 创建应用并记录 `api_id` 和 `api_hash`。
4. 不要把 `api_hash` 上传到 GitHub 或发送给其他人。

### 2. 创建报告机器人

1. 在 Telegram 中打开 `@BotFather`。
2. 发送 `/newbot`。
3. 按提示设置机器人名称和用户名。
4. 保存 BotFather 返回的机器人 Token。
5. 打开刚创建的机器人并发送 `/start`。

机器人只负责发送运行报告，不负责读取或发布频道内容。频道运营继续使用 Telethon 用户账号。

### 3. 获取私人 chat ID

先向机器人发送 `/start`，然后临时把机器人 Token 放入环境变量并读取更新：

```bash
export TG_REPORT_BOT_TOKEN='123456:替换为真实Token'
curl -sS -X POST "https://api.telegram.org/bot${TG_REPORT_BOT_TOKEN}/getUpdates" \
  | python3 -m json.tool
```

在输出中查找：

```json
{
  "message": {
    "chat": {
      "id": 123456789
    }
  }
}
```

这个正整数就是私人 `chat_id`。把一个或多个 ID 写入 `config.toml` 的 `[reporting] chat_ids` 数组。不要公开完整的 `getUpdates` 输出，因为其中可能包含私人消息和账号信息。

### 4. 准备频道权限

- 用户账号必须已经加入每一个源私密频道。
- 用户账号必须可以读取源频道历史。
- 用户账号必须是目标频道管理员，且具有发布消息权限。
- 报告机器人不需要加入源频道或目标频道。
- 频道 ID 通常是以 `-100` 开头的负整数，也可以使用公开频道用户名。

## 本地源码运行（可选）

本节只用于开发调试或暂时不使用 Docker 的环境；生产部署请直接阅读后面的 Docker 章节。以下命令假定当前使用 root，先进入实际项目目录：

```bash
cd /opt/telegram/TG-Channels-Automated-Operation
```

安装系统依赖：

```bash
apt update
apt install -y python3.12 python3.12-venv ffmpeg git curl
```

确认版本：

```bash
python3.12 --version
ffmpeg -version | head -n 1
ffprobe -version | head -n 1
```

创建虚拟环境并安装项目：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[speed]'
```

`[speed]` 会安装 `cryptg`，用于加速 Telethon 的加密处理。项目的基础依赖还包括 Telethon、hachoir、HTTPX 和时区数据。

复制配置模板：

```bash
cp .env.example .env
cp config.example.toml config.toml
chmod 600 .env
```

然后编辑：

```bash
nano .env
nano config.toml
```

## `.env` 完整说明

`.env` 必须与传给 `--config` 的配置文件位于同一个目录。统一部署时，`config.toml` 和 `.env` 都放在 `/opt/telegram/TG-Channels-Automated-Operation/`。

完整示例：

```dotenv
TG_API_ID=123456
TG_API_HASH=0123456789abcdef0123456789abcdef
TG_PHONE=+8613800000000
TG_SESSION_PATH=./data/telegram-user
TG_REPORT_BOT_TOKEN=123456:ABCDEF_replace_me
TZ=Asia/Shanghai
```

字段说明：

| 变量 | 必填 | 示例 | 说明 |
|---|---|---|---|
| `TG_API_ID` | 是 | `123456` | 从 my.telegram.org 获取的整数 API ID。 |
| `TG_API_HASH` | 是 | `0123...cdef` | 从 my.telegram.org 获取的 API Hash，属于敏感凭据。 |
| `TG_PHONE` | 登录时建议 | `+8613800000000` | Telegram 用户账号手机号，使用带国家区号的 E.164 格式。 |
| `TG_SESSION_PATH` | 建议显式设置 | `./data/telegram-user` | Telethon 用户会话路径；相对路径以 `config.toml` 所在目录为基准。通常不要手工添加 `.session` 后缀，父目录必须可写。 |
| `TG_REPORT_BOT_TOKEN` | 是 | `123456:ABC...` | BotFather 提供的报告机器人 Token。 |
| `TZ` | Docker 建议 | `Asia/Shanghai` | 设置容器日志时区；建议与 `[schedule] timezone` 保持一致。任务触发时间仍以 TOML 为准。 |

注意事项：

- `.env` 支持空行、以 `#` 开头的注释、引号和 `export KEY=value` 格式。
- 如果系统环境中已经存在同名变量，系统环境变量优先，`.env` 不会覆盖它。
- `.env`、`.session`、数据库和 Token 都不能提交到 Git。
- Telethon 会话文件等同于登录凭据。泄露后应立即撤销相关会话。
- 报告机器人 Token 泄露后应通过 BotFather 立即重新生成。

## `config.toml` 完整说明

项目使用 TOML 配置。所有相对路径都相对于 `config.toml` 所在目录解析，而不是相对于当前 Shell 目录解析。

### `[content]` 文案和标签

```toml
[content]
keep_tags = ["#必留标签", "#中文字幕", "#欧美精选"]
drop_tags = ["#广告", "#删除标签"]
caption_limit = 1024
```

#### `keep_tags`

- 优先保留标签库，可以配置几十个或更多。
- 只保留原始文案中真实存在的标签，不会主动把缺失标签补进文案。
- 如果原文中出现多个 `keep_tags`，按配置顺序优先采用。
- 单篇最终文案最多保留 5 个标签。

#### `drop_tags`

- 原文中命中的标签会被删除。
- `keep_tags` 和 `drop_tags` 经过 Unicode 规范化并忽略大小写后不能重叠，否则配置加载失败。

#### `caption_limit`

- 目标媒体说明长度限制，允许范围为 `1–1024`，默认和推荐值为 `1024`。
- Telegram 当前对普通用户的媒体说明限制为 1024；Premium 用户的 MTProto 配置上限更高，但本项目固定以 1024 为最高值，以兼容普通账号和不同发送环境。可参考 Telegram 的[客户端配置说明](https://core.telegram.org/api/config)。
- 超出限制时优先完整保留标签，只缩短简介，并在被省略内容的位置以 `...` 结尾。
- `...` 本身也计入 1024 的长度，因此最终发送的文案不会因为添加省略号而超过限制。
- 长度按 Telegram 实体使用的 UTF-16 单元计算；中文、英文通常各占 1，部分 Emoji 占 2。

源文案支持以下标签行：

```text
#标签1 #标签2 #标签3
```

也支持：

```text
标签：#中文字幕 #Wifey #欧美精选 @kakasp
```

`@用户名` 不作为标签处理。简介只读取以下格式的当前行：

```text
简介：这里是简介内容
简介: 这里是简介内容
```

其他行不会保留。简介发送时使用 Telegram HTML 引用格式。标签和简介都不存在时，该源媒体组会在下载前永久跳过，并自动选择替补。

### `[schedule]` 时区和运行日期

```toml
[schedule]
timezone = "Asia/Shanghai"
daily_time = "00:01"
```

#### `timezone`

- 使用 IANA 时区名称，例如 `Asia/Shanghai`。
- 用于计算“今天”、每日完成数量和运行摘要日期。

#### `daily_time`

- 必须使用 24 小时制 `HH:MM` 格式。
- Docker 中的 `schedule` 常驻命令会按这个时间自动运行，不需要另外配置 cron 或 systemd timer。
- 容器在当天计划时间之前启动时会等待到点；在计划时间之后启动时会立即补跑一次。
- 修改时间或时区后执行 `docker compose restart channel-operator`，让常驻调度器重新读取配置。

### `[reporting]` 机器人报告

```toml
[reporting]
chat_ids = [
  123456789,
  987654321,
]
```

#### `chat_ids`

- 接收报告的私人 Telegram chat ID 数组，至少配置一个。
- 每个值都必须是互不重复的正整数。
- 每个接收人都必须先向报告机器人发送 `/start`，否则机器人无法主动给该用户发送消息。
- 所有告警和最终摘要都会按配置顺序发送给数组中的每个接收人。
- `doctor` 会调用 Bot API 验证 Token，并向每个 `chat_id` 真实发送一条测试消息。
- 旧写法 `chat_id = 123456789` 仍兼容，但不能与 `chat_ids` 同时配置；新配置建议统一使用 `chat_ids`。

机器人发送的内容包括：

- 频道组被封、不可访问、权限不足或数据库异常时的即时告警。
- 上传结果无法确认时的即时告警。
- 全部频道组运行结束后的统一摘要。
- 每组的成功数、目标数、尝试数、永久跳过、可重试失败和恢复确认数量。

机器人报告失败会写入本地日志，但不会中止频道下载或发布。

### `[processing]` 视频处理

```toml
[processing]
ffmpeg_path = "ffmpeg"
ffprobe_path = "ffprobe"
ffmpeg_threads = 4
crf = 24
preset = "medium"
audio_bitrate = "128k"
minimum_source_short_edge = 1080
album_settle_seconds = 300
disk_reserve_bytes = 1073741824
```

| 字段 | 默认值 | 说明 |
|---|---:|---|
| `ffmpeg_path` | `ffmpeg` | FFmpeg 可执行文件名或绝对路径。 |
| `ffprobe_path` | `ffprobe` | FFprobe 可执行文件名或绝对路径。 |
| `ffmpeg_threads` | `3` | FFmpeg 编码线程数。本项目针对 4 核 VPS 限制为 `1–4`；示例使用 `4`。 |
| `crf` | `23` | H.264 恒定质量参数，允许 `0–51`。数值越小质量越高、文件越大；示例使用 `24`。 |
| `preset` | `medium` | x264 编码速度预设。越慢通常压缩效率越高。 |
| `audio_bitrate` | `128k` | AAC 音频码率。 |
| `minimum_source_short_edge` | `1080` | 源视频短边至少达到该像素数才可参与选择。 |
| `album_settle_seconds` | `300` | 媒体组最后一条消息发布后至少等待多少秒再处理，避免索引到尚未发送完整的媒体组。 |
| `disk_reserve_bytes` | `1073741824` | 除“源文件大小 × 3”外额外保留的磁盘空间，示例为 1 GiB。 |

转码规则：

- 视频编码：H.264 `libx264`。
- 音频编码：AAC。
- 像素格式：`yuv420p`。
- MP4：`+faststart`。
- 横屏最大边界：`1280×720`。
- 竖屏最大边界：`720×1280`。
- 保持宽高比并确保输出尺寸为偶数。
- 转码、截图和媒体组全部串行处理。

### `[runtime]` 运行限制和工作目录

```toml
[runtime]
database_dir = "./data"
work_dir = "./work"
max_candidates_per_run = 12
max_runtime_hours = 6
download_concurrency = 4
flood_sleep_threshold_seconds = 60
retry_delays_seconds = [30, 120, 600]
```

#### `database_dir`

- 所有频道组 SQLite 数据库的统一保存目录。
- 默认值为 `./data`；相对路径以 `config.toml` 所在目录为基准。
- 数据库文件名不再手工配置，而是自动使用 `<频道组名称>.db`。
- 例如统一部署目录中使用 `database_dir = "./data"` 且组名为 `channel_b`，实际数据库为 `/opt/telegram/TG-Channels-Automated-Operation/data/channel_b.db`。
- 生产环境运行用户必须拥有该目录的读写权限。

#### `work_dir`

- 下载、转码、截图和全局锁文件所在目录。
- 每个频道组使用 `work_dir/<频道组名称>/` 子目录。
- 成功、失败或任务取消后会尽力清理当前媒体临时文件。
- 目录所在文件系统必须具有足够空间。

#### `max_candidates_per_run`

- 每个频道组在一次运行中最多尝试多少个候选媒体组。
- 该额度进入下一个频道组时会重新计算。
- 必须大于或等于每个频道组的 `daily_success_count`。
- 示例为 12，允许为了成功发布 4 组而使用无效素材替补。

#### `max_runtime_hours`

- 每个频道组单次最多运行多少小时。
- 进入下一个频道组时重新计时。
- 多个频道组的总运行时间理论上可能超过这个值。例如五组、每组最多 6 小时，总任务极端情况下可能接近 30 小时。

#### `download_concurrency`

- 单个视频同时进行多少路交错分片下载。
- 允许范围 `1–8`，默认和推荐起点为 `4`。
- 这不是同时处理多个媒体组；只是对当前视频同时发出多个分片请求。
- 高延迟线路通常能从 4 路或 8 路获得明显提升。
- 如果 FloodWait、超时或断点重试明显增加，应降低为 4 或 2。

#### `flood_sleep_threshold_seconds`

- Telethon 自动等待短 FloodWait 的阈值。
- 允许范围 `1–86400` 秒，默认 60 秒。
- 超过阈值或重试耗尽后，由项目自己的错误与续传逻辑处理。

#### `retry_delays_seconds`

- 网络类错误的重试间隔列表，单位为秒。
- `[30, 120, 600]` 表示首次失败后等 30 秒、第二次等 2 分钟、第三次等 10 分钟。
- 同一次上传重试使用已经保存的同一份随机文案。

### `[[channel_groups]]` 独立频道组

每出现一次 `[[channel_groups]]` 就定义一个源频道到目标频道的独立运营关系：

```toml
[[channel_groups]]
name = "channel_b"
source_channel = -1001234567890
target_channel = -1009876543210
daily_success_count = 4
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | 是 | 频道组唯一名称，长度 1–64，只允许字母、数字、下划线和连字符，且首字符必须是字母或数字。 |
| `source_channel` | 是 | 源频道数字 ID 或用户名。多个频道组可以使用同一个源频道。 |
| `target_channel` | 是 | 目标频道数字 ID 或用户名。用户账号必须具有发帖权限。 |
| `daily_success_count` | 是 | 当前频道组每天希望达到的成功发布数量，必须大于 0 且不能超过 `max_candidates_per_run`。 |

不允许再在 `[[channel_groups]]` 中配置 `database_path`。每个组的 `name` 必须唯一，程序会在全局 `database_dir` 下自动创建同名数据库。例如：

```text
channel_b → ./data/channel_b.db
channel_c → ./data/channel_c.db
```

频道组的书写顺序就是运行顺序。以下配置一定先完成 `channel_b`，再开始 `channel_c`：

```toml
[[channel_groups]]
name = "channel_b"
# ...

[[channel_groups]]
name = "channel_c"
# ...
```

每日数量是数据库状态，不是简单的“每次命令再发送 N 组”。例如 `channel_b` 的目标为 4：

- 今天已经成功 0 组：本次最多补到 4。
- 今天已经成功 3 组：本次只需要再成功 1 组。
- 今天已经成功 4 组：本次不再发布，随后进入下一频道组。
- 明天运行：按新日期重新以 4 组为目标。

每个数据库还会保存频道组名称、源频道和目标频道身份。如果修改了已有组的源频道或目标频道，但继续使用相同组名，程序会拒绝运行该组并通过机器人告警，避免状态串用。

## 五个目标频道的完整配置示例

下面是源频道 A 分发到 B、C、D、E、F 的完整结构。请替换所有示例 ID：

```toml
[content]
keep_tags = [
  "#中文字幕",
  "#欧美精选",
  "#欧美剧情",
  "#Wifey",
]
drop_tags = ["#广告", "#旧标签"]
caption_limit = 1024

[schedule]
timezone = "Asia/Shanghai"
daily_time = "00:01"

[reporting]
chat_ids = [123456789, 987654321]

[processing]
ffmpeg_path = "ffmpeg"
ffprobe_path = "ffprobe"
ffmpeg_threads = 4
crf = 24
preset = "medium"
audio_bitrate = "128k"
minimum_source_short_edge = 1080
album_settle_seconds = 300
disk_reserve_bytes = 1073741824

[runtime]
database_dir = "./data"
work_dir = "./work"
max_candidates_per_run = 12
max_runtime_hours = 6
download_concurrency = 4
flood_sleep_threshold_seconds = 60
retry_delays_seconds = [30, 120, 600]

[[channel_groups]]
name = "channel_b"
source_channel = -1001111111111
target_channel = -1002222222222
daily_success_count = 4

[[channel_groups]]
name = "channel_c"
source_channel = -1001111111111
target_channel = -1003333333333
daily_success_count = 4

[[channel_groups]]
name = "channel_d"
source_channel = -1001111111111
target_channel = -1004444444444
daily_success_count = 3

[[channel_groups]]
name = "channel_e"
source_channel = -1001111111111
target_channel = -1005555555555
daily_success_count = 2

[[channel_groups]]
name = "channel_f"
source_channel = -1001111111111
target_channel = -1006666666666
daily_success_count = 1
```

这个例子每天最多成功发布：

```text
B：4 组
C：4 组
D：3 组
E：2 组
F：1 组
合计：14 组
```

这些内容不会同时处理。顺序固定为 B 的所有任务结束后处理 C，然后处理 D、E、F。

## 命令行完整说明

通用格式：

```bash
.venv/bin/channel-operator [全局选项] <命令> [命令选项]
```

### 全局选项

#### `--config`

指定 TOML 配置文件路径：

```bash
.venv/bin/channel-operator --config config.toml doctor
```

生产环境示例：

```bash
docker compose run --rm channel-operator \
  --config /app/config.toml doctor
```

如果省略，默认读取当前目录的 `config.toml`。

#### `--verbose`

启用调试日志。这个选项必须放在子命令前：

```bash
.venv/bin/channel-operator --config config.toml --verbose run-once --group channel_b
```

项目会抑制 HTTPX/httpcore 的请求 URL 日志，避免 Bot API Token 因为位于 URL 中而被输出。

### `login`

交互登录 Telethon 用户账号：

```bash
.venv/bin/channel-operator --config config.toml login
```

首次运行会要求输入：

- Telegram 登录验证码。
- 开启两步验证时的密码。

登录完成后会在 `TG_SESSION_PATH` 对应位置生成 `.session` 文件。只需要登录用户账号，报告机器人使用 Bot API Token，不需要执行第二次 Telethon 登录。

### `doctor`

检查全部频道组：

```bash
.venv/bin/channel-operator --config config.toml doctor
```

只检查指定频道组：

```bash
.venv/bin/channel-operator --config config.toml doctor --group channel_b
```

检查内容包括：

- FFmpeg 和 FFprobe 是否可执行。
- Telethon 用户会话是否已经登录。
- 报告机器人 Token 是否有效。
- 报告机器人能否向所有私人 `chat_ids` 发送消息。
- 源频道和目标频道是否可解析。
- 用户账号是否具有目标频道发帖权限。
- 数据库目录是否可写。
- 数据库身份是否与频道组匹配。
- 工作目录和当前下载并发配置。

重要：`doctor` 会真实发送一条“报告测试成功”消息，也会创建尚不存在的频道组数据库文件并写入身份信息。

### `index`

索引全部频道组：

```bash
.venv/bin/channel-operator --config config.toml index
```

只索引指定频道组：

```bash
.venv/bin/channel-operator --config config.toml index --group channel_b
```

首次执行会扫描源频道历史；以后根据当前频道组数据库中的检查点只扫描新消息。即使多个频道组使用同一个源频道，它们仍分别维护自己的索引和检查点。

如果一个频道组索引失败，程序通过机器人报告并继续索引后面的组。

### `run-once --dry-run`

预览全部频道组：

```bash
.venv/bin/channel-operator --config config.toml run-once --dry-run
```

预览指定频道组：

```bash
.venv/bin/channel-operator --config config.toml run-once --dry-run --group channel_b
```

dry-run 会：

- 建立或更新源频道索引。
- 随机选择符合条件且未成功发布的候选。
- 应用标签过滤和简介提取规则。
- 输出候选 `grouped_id` 和最终文案。
- 过滤处理后文案为空的候选。

dry-run 不会：

- 下载视频。
- 转码或截图。
- 上传媒体。
- 将候选标记为已选择或已发布。

注意：dry-run 会更新 SQLite 中的索引和检查点，因此并不是完全不写数据库。

### `run-once`

正式运行全部频道组：

```bash
.venv/bin/channel-operator --config config.toml run-once
```

只正式运行一个频道组：

```bash
.venv/bin/channel-operator --config config.toml run-once --group channel_b
```

程序会先检查当前组当天已经成功发布多少组，再补足到 `daily_success_count`。全部频道组结束后，报告机器人发送统一摘要。

### `schedule`

常驻运行，并按照 `[schedule]` 中的 `timezone` 和 `daily_time` 每天调用一次完整 `run-once`：

```bash
.venv/bin/channel-operator --config config.toml schedule
```

Docker 镜像默认执行这个命令。行为如下：

- 启动时间早于当天计划时间：等待到计划时间执行。
- 启动时间晚于或等于当天计划时间：立即补跑一次。
- 一次任务完成或部分频道组失败后：调度器保持运行，等待下一天。
- 容器重启后即使再次触发当天任务，SQLite 每日计数也只会补足差额，不会重复发布已经成功的素材。
- 修改 `config.toml` 的运行时间后需要重启常驻进程。

### 退出码

| 退出码 | 含义 |
|---:|---|
| `0` | 所有选中的频道组达到当日目标，或命令成功完成。 |
| `1` | 公共配置、登录、报告机器人 doctor、全局锁等错误导致任务无法正常启动。 |
| `2` | 至少一个频道组未达到目标、被跳过，或者 `doctor/index/dry-run` 中至少一组失败。 |
| `130` | 用户使用 `Ctrl+C` 中断任务。 |

Shell 中查看上一条命令退出码：

```bash
echo $?
```

## 首次运行顺序

建议严格按以下顺序执行。

### 1. 检查配置语法但暂不启动常驻容器

确保 `.env` 和 `config.toml` 已填写，然后登录：

```bash
docker compose run --rm channel-operator \
  --config /app/config.toml login
```

### 2. 执行 doctor

```bash
docker compose run --rm channel-operator \
  --config /app/config.toml doctor
```

确认：

- FFmpeg/FFprobe 输出正常。
- 每个频道组的 `post_permission` 为 `ok`。
- 私人 Telegram 会话收到机器人测试消息。
- 每个频道组显示的自动数据库路径正确。

### 3. 建立索引

```bash
docker compose run --rm channel-operator \
  --config /app/config.toml index
```

首次索引可能需要较长时间。各频道组数据库独立，因此相同源频道会分别扫描和保存状态。

### 4. 预览选材和文案

```bash
docker compose run --rm channel-operator \
  --config /app/config.toml run-once --dry-run
```

也可以先只测试一个组：

```bash
docker compose run --rm channel-operator \
  --config /app/config.toml run-once --dry-run --group channel_b
```

### 5. 在测试频道正式发布

建议先把目标 ID 配置为测试频道，并为测试关系使用一个全新的频道组名称，使程序自动创建新的测试数据库：

```bash
docker compose run --rm channel-operator \
  --config /app/config.toml run-once --group channel_b
```

确认 Telegram 中满足：

- 一个视频加三张图片显示为一个媒体组。
- 视频位于第一项。
- 文案只在视频上。
- 视频封面和比例正确。
- 视频能够流式播放。
- 数据库和工作目录没有异常。

验证完成后再启动 Docker 常驻调度器。

## 多频道串行、数据库和失败处理

### 严格串行

程序不会同时处理两个媒体组，也不会同时运行两个频道组。示例顺序：

```text
channel_b 媒体1：下载 → 转码 → 截图 → 上传 → 清理
channel_b 媒体2：下载 → 转码 → 截图 → 上传 → 清理
channel_b 完成
channel_c 媒体1：下载 → 转码 → 截图 → 上传 → 清理
channel_c 完成
```

下载器内部的 `download_concurrency` 只是当前单个视频的分片并发，不改变媒体组和频道组的串行规则。

### 独立数据库

每个频道组的 SQLite 包含：

- 源消息索引。
- 媒体组索引。
- 增量扫描检查点。
- 选择日期和处理状态。
- 尝试次数。
- 上传开始时间和固定文案。
- 目标消息 ID 和目标 `grouped_id`。
- 发布日期和错误信息。
- 频道组名称、源频道和目标频道身份。

运行期间可能同时看到：

```text
channel_b.db
channel_b.db-wal
channel_b.db-shm
```

`-wal` 和 `-shm` 是 SQLite WAL 模式的正常文件。程序运行时不要删除、移动或编辑它们。

### 频道组级错误

以下情况会终止当前频道组、即时机器人告警并继续下一组：

- 频道被 Telegram 封禁。
- 用户账号被移出或封禁于源频道。
- 私密频道无法访问。
- 频道 ID 无效或频道已删除。
- 目标频道禁止写入。
- 用户账号失去管理员或发帖权限。
- 当前组数据库损坏、无法打开或身份不匹配。
- 当前组发生未被素材级逻辑处理的异常。

被跳过的频道组不会永久禁用。下一次定时运行会重新检查，频道恢复后可以继续。

### 素材级错误

以下情况通常只影响当前媒体，不会跳过整个频道组：

- 处理后文案为空。
- 视频短边不足。
- 源消息被删除或不再是视频。
- 视频元数据无效。
- FFmpeg/FFprobe 无法处理当前文件。
- 当前媒体下载或上传的临时网络重试耗尽。

程序会记录失败，并在候选数量和运行时间允许时选择替补。

### 上传结果不确定

如果网络在 Telegram 已经接收媒体后、客户端收到响应前断开，盲目重试可能造成重复发帖。程序会检查目标频道近期媒体组：

- 唯一匹配时，恢复并记录成功结果。
- 无法确认时，暂停该源媒体并通过报告机器人要求人工核对。

### 下载续传

- 下载写入 `source_video.mp4.part`。
- 只把完整、连续的 512 KiB 批次写入临时文件。
- 同一次媒体处理过程中的网络重试会从安全的 512 KiB 边界恢复，不会重新下载已经完整写入的部分。
- 下载完成并验证文件大小后，原子替换为 `source_video.mp4`。
- 多路下载失败不会把分片按错误顺序拼接。
- 当前媒体最终失败、任务取消或处理结束后，所属临时目录会被清理；下一次独立运行通常会重新下载该媒体。

## Docker 生产部署

生产环境推荐直接使用 Docker Engine 和 Docker Compose。Docker 负责 Python、Telethon、hachoir、cryptg 和 FFmpeg 依赖，宿主机只保留源码、真实配置和持久化数据，不再需要为本项目安装 Python 虚拟环境或复制 systemd unit。

统一目录如下：

```text
/opt/telegram/TG-Channels-Automated-Operation/
├── Dockerfile               # 生产镜像定义
├── compose.yaml             # 容器、资源、挂载和日志设置
├── config.toml              # 实际运行配置，不提交 Git
├── .env                     # Telegram 凭据，不提交 Git
├── data/                    # Telethon Session 和各频道组 SQLite
├── work/                    # 下载、转码、截图和全局进程锁
├── deploy/
│   └── config.production.toml
└── src/
```

`deploy/config.production.toml` 只是首次创建 `config.toml` 时使用的模板，程序和容器都不会自动读取它。容器实际读取项目根目录的 `config.toml` 和 `.env`。

下面的 VPS 命令假定当前直接使用 root。

### 1. 安装并检查 Docker

安装 Docker Engine 和 Docker Compose plugin 后确认：

```bash
docker --version
docker compose version
docker info
```

应使用 `docker compose`，而不是已经停止维护的旧命令 `docker-compose`。Docker 服务需要处于运行状态：

```bash
systemctl enable --now docker
```

### 2. 获取项目源码

首次部署：

```bash
mkdir -p /opt/telegram
git clone YOUR_REPOSITORY_URL \
  /opt/telegram/TG-Channels-Automated-Operation
cd /opt/telegram/TG-Channels-Automated-Operation
```

源码已经存在时：

```bash
cd /opt/telegram/TG-Channels-Automated-Operation
git status --short --branch
git pull --ff-only
```

### 3. 创建真实配置和持久化目录

以下命令不会覆盖已经存在的配置：

```bash
test -f config.toml || cp deploy/config.production.toml config.toml
test -f .env || cp .env.example .env
mkdir -p data work
chmod 600 config.toml .env
chmod 700 data work
```

编辑：

```bash
nano .env
nano config.toml
```

`.env` 示例：

```dotenv
TG_API_ID=123456
TG_API_HASH=替换为真实API_HASH
TG_PHONE=+8613800000000
TG_SESSION_PATH=./data/telegram-user
TG_REPORT_BOT_TOKEN=替换为真实机器人Token
TZ=Asia/Shanghai
```

`config.toml` 必须使用容器内可解析的相对持久化目录：

```toml
[schedule]
timezone = "Asia/Shanghai"
daily_time = "00:01"

[runtime]
database_dir = "./data"
work_dir = "./work"
```

相对路径以容器中的 `/app/config.toml` 为基准，因此最终分别对应 `/app/data` 和 `/app/work`；这两个目录又绑定到宿主机项目目录中的 `data/` 和 `work/`。

每个频道组仍只配置唯一 `name`。数据库自动保存为 `data/<name>.db`，不配置 `database_path`。

### 4. 理解 Compose 的持久化和资源设置

项目提供的 `compose.yaml` 使用四个 bind mount：

| 宿主机路径 | 容器路径 | 权限 | 用途 |
|---|---|---|---|
| `./config.toml` | `/app/config.toml` | 只读 | 主配置 |
| `./.env` | `/app/.env` | 只读 | API、账号和机器人凭据 |
| `./data` | `/app/data` | 读写 | Session 与 SQLite |
| `./work` | `/app/work` | 读写 | 临时媒体和进程锁 |

重建或更新容器不会删除 `data/`，因此不会丢失登录 Session、索引和发布记录。`work/` 里的单个媒体临时目录仍由程序在处理结束时清理。

默认资源：

```yaml
cpus: 4.0
mem_limit: 5g
```

这适合 4 核 6 GiB VPS，并为宿主机和 Docker daemon 留出约 1 GiB 内存。`ffmpeg_threads = 4` 仍然是 FFmpeg 自身的编码线程限制。容器不会获得超过宿主机实际数量的 CPU；如果 VPS 以后改为 2 核，应同时降低 `cpus` 和 `ffmpeg_threads`。

Compose 还启用了：

- `restart: unless-stopped`：VPS 或 Docker 重启后自动恢复调度器。
- `init: true`：正确转发停止信号并回收子进程。
- `stop_grace_period: 10m`：给 Telethon、FFmpeg 和清理逻辑留出退出时间。
- `read_only: true`：容器镜像根文件系统只读。
- `cap_drop: [ALL]` 和 `no-new-privileges`。
- JSON 日志每个文件最多 20 MiB，最多保留 5 个。
- 容器内默认使用 root；只有挂载的 `data/` 和 `work/` 用于持久写入。

### 5. 构建镜像

```bash
cd /opt/telegram/TG-Channels-Automated-Operation
docker compose build
```

镜像构建时会安装 Python 3.12、Telethon、hachoir、cryptg、HTTPX、FFmpeg 和时区数据。检查最终镜像：

```bash
docker image ls telegram-channel-operator
docker compose config
```

`docker compose config` 不应报告缺少 `config.toml`、`.env`、`data` 或 `work`。

### 6. 在一次性容器中登录 Telegram

在启动常驻调度器前执行：

```bash
docker compose run --rm channel-operator \
  --config /app/config.toml login
```

根据提示输入验证码和两步验证密码。成功后检查宿主机文件：

```bash
ls -lh data/telegram-user.session
chmod 600 data/telegram-user.session
```

`--rm` 只删除这次登录使用的临时容器，不会删除 bind mount 中的 Session。

### 7. 在一次性容器中检查、索引和预览

依次执行：

```bash
docker compose run --rm channel-operator \
  --config /app/config.toml doctor

docker compose run --rm channel-operator \
  --config /app/config.toml index

docker compose run --rm channel-operator \
  --config /app/config.toml run-once --dry-run
```

只检查或预览某个频道组：

```bash
docker compose run --rm channel-operator \
  --config /app/config.toml doctor --group channel_b

docker compose run --rm channel-operator \
  --config /app/config.toml run-once --dry-run --group channel_b
```

`doctor` 会真实向全部报告接收人发送测试消息。确认频道权限、数据库路径、FFmpeg 和报告机器人全部正常后，再启动常驻容器。

### 8. 启动每日自动任务

```bash
docker compose up -d
docker compose ps
docker compose logs --tail=100 channel-operator
```

镜像默认运行：

```bash
channel-operator --config /app/config.toml schedule
```

调度规则：

- 容器在当天 `daily_time` 之前启动：等待到点运行。
- 容器在当天 `daily_time` 之后启动：立即补跑一次。
- 每次运行都执行全部频道组，完成后发送机器人汇总。
- 一组失败不会结束常驻调度器，下一天会继续检查。
- 容器重启后再次补跑也会先读取 SQLite 的当天完成数，只补足差额。
- 修改 `daily_time`、`timezone` 或频道组后，执行 `docker compose restart channel-operator` 重新加载配置。

### 9. 查看日志和资源

实时日志：

```bash
docker compose logs -f channel-operator
```

最近 300 行：

```bash
docker compose logs --tail=300 channel-operator
```

查看日志时间范围：

```bash
docker compose logs \
  --since "2026-08-09T00:00:00+08:00" \
  --until "2026-08-10T00:00:00+08:00" \
  channel-operator
```

观察 CPU、内存、网络和磁盘 I/O：

```bash
docker stats channel-operator
```

在 Docker 的 CPU 显示中，多核容器可能超过 100%。例如四核上的 320% 大约表示使用了三点二个核心，不代表超出服务器能力。

### 10. 手工立即运行

常驻容器已经运行时，可以在同一个容器内立即执行：

```bash
docker compose exec channel-operator \
  channel-operator --config /app/config.toml run-once
```

只运行一个组：

```bash
docker compose exec channel-operator \
  channel-operator --config /app/config.toml run-once --group channel_b
```

如果定时任务正在运行，全局锁会拒绝第二个任务，不会并行下载或转码。不要启动多个使用不同 `work_dir` 但共享数据库的容器来绕过锁。

### 11. 停止和重新启动

优雅停止：

```bash
docker compose stop -t 600
```

再次启动：

```bash
docker compose start
```

重新创建容器但保留数据：

```bash
docker compose up -d --force-recreate
```

停止并删除容器和项目网络：

```bash
docker compose down
```

`docker compose down` 不会删除 bind mount 中的 `data/`、`work/`、`config.toml` 和 `.env`。不要手工删除 `data/`，否则会丢失 Session、索引和防重复发布记录。

### 12. 从旧 systemd 部署切换

先停用旧定时器，避免 systemd 与 Docker 在同一时刻启动两个任务：

```bash
systemctl disable --now channel-operator.timer
systemctl stop channel-operator.service
```

如果旧版已经使用当前项目根目录下的 `config.toml`、`.env`、`data/` 和 `work/`，无需迁移数据，直接构建并启动 Docker。

如果旧数据仍位于 `/var/lib/channel-operator`，旧配置仍位于 `/etc/channel-operator`，先在 Docker 尚未启动时复制，并且不覆盖已经存在的文件：

```bash
cd /opt/telegram/TG-Channels-Automated-Operation
mkdir -p data work

if [ ! -f config.toml ] && [ -f /etc/channel-operator/config.toml ]; then
  cp -a /etc/channel-operator/config.toml config.toml
fi
if [ ! -f .env ] && [ -f /etc/channel-operator/.env ]; then
  cp -a /etc/channel-operator/.env .env
fi
if [ -d /var/lib/channel-operator ]; then
  cp -a -n /var/lib/channel-operator/. data/
fi

chmod 600 config.toml .env
chmod 700 data work
```

随后把 `TG_SESSION_PATH`、`database_dir`、`work_dir` 分别改成 `./data/telegram-user`、`./data`、`./work`。通过登录检查、`doctor` 和 dry-run 后再启动 Compose。确认 Docker 至少完成一次正式任务之前，保留旧目录作为回退副本。

旧 unit 文件留在 `/etc/systemd/system` 不会占用资源，只要 timer 已经 disabled。确认不再回退 systemd 后可以自行删除。

## 更新源码和升级

### 1. 确认当前任务处于等待状态

先查看日志和进程：

```bash
cd /opt/telegram/TG-Channels-Automated-Operation
docker compose logs --tail=100 channel-operator
docker top channel-operator
```

如果看到 FFmpeg、上传或下载仍在运行，建议等待当前媒体组自然完成后再更新。

### 2. 停止容器并备份

```bash
docker compose stop -t 600

mkdir -p /var/backups/channel-operator/2026-08-09
cp -a config.toml .env data \
  /var/backups/channel-operator/2026-08-09/
```

把日期替换为实际日期；目录已经存在时增加时间后缀，避免覆盖以前的备份。`work/` 是可重建的临时目录，通常不备份。

### 3. 拉取源码并重建

```bash
git status --short --branch
git pull --ff-only
docker compose build --pull
docker compose up -d --remove-orphans
```

`git pull` 不会覆盖被 `.gitignore` 排除的真实 `config.toml`、`.env`、`data/` 和 `work/`。`docker compose up` 重建容器时也会保留这些 bind mount 数据。

如果显示 `Already up to date.`，说明远程仓库没有更新提交，需要先在开发机器提交并推送。

### 4. 验证升级

```bash
docker compose ps
docker compose logs --tail=200 channel-operator

docker compose exec channel-operator \
  channel-operator --config /app/config.toml doctor
```

如果只修改 `config.toml` 或 `.env`，不需要重建镜像，但需要重启调度器：

```bash
docker compose restart channel-operator
```

如果修改 Python 依赖、源码、Dockerfile 或 Compose，使用：

```bash
docker compose up -d --build --remove-orphans
```

### 5. 清理旧镜像

确认新容器运行正常后，可以清理没有被任何容器引用的悬空镜像：

```bash
docker image prune
```

此命令不会删除正在使用的镜像，也不会删除项目的 bind mount 数据。执行前仍应先检查 `docker image ls`。

## 日志与故障排查

### `Already up to date.` 但 VPS 没有新功能

原因通常是开发机器的修改尚未提交或推送。开发机器依次执行：

```bash
git status --short --branch
git add README.md config.example.toml deploy pyproject.toml src tests .env.example
git commit -m "feat: support multi-channel operation and bot reports"
git push origin main
```

然后 VPS 再执行：

```bash
git pull --ff-only
```

### `必须配置至少一个 [[channel_groups]]`

说明仍在使用旧版单频道 TOML。参照本文完整示例，把源频道、目标频道和每日数量移动到 `[[channel_groups]]`，数据库目录配置到 `[runtime] database_dir`。

### `TG_REPORT_BOT_TOKEN` 缺失或 `Unauthorized`

- 检查 `.env` 是否与 `config.toml` 位于同一目录。
- 检查 Token 是否完整，不能包含多余空格。
- 检查 BotFather 是否已经撤销或重新生成 Token。
- 修改 Token 后重新运行 `doctor`。

### 报告机器人提示 `chat not found` 或部分接收人收不到消息

- 让 `chat_ids` 数组中的每个接收人分别打开机器人并发送 `/start`。
- 确认每个 ID 都是对应用户的私人正整数 ID，不是频道 ID。
- 分别让接收人向机器人发送消息，再调用 `getUpdates` 检查 ID。
- 检查数组中没有重复值，也没有同时保留旧的 `chat_id`。
- 运行 `doctor`；它会向数组中的每个接收人真实发送测试消息。

### `Telethon 会话尚未登录`

执行：

```bash
docker compose run --rm channel-operator \
  --config /app/config.toml login
```

登录容器和常驻容器挂载同一个宿主机 `data/`，因此生成的 Session 会被正式任务直接使用。不要把 `TG_SESSION_PATH` 配置到 `/tmp` 或容器镜像内部。

### 频道组被跳过

查看机器人告警和日志中的错误类型：

- `ChannelBannedError`：频道被 Telegram 封禁。
- `ChannelPrivateError`：频道私有、账号不在频道中或账号被移出/封禁。
- `ChannelInvalidError`：频道 ID 无效或实体不可用。
- `ChatWriteForbiddenError`：目标频道不允许当前账号写入。
- `ChatAdminRequiredError`：缺少管理员权限。
- `DatabaseIdentityError`：数据库属于其他频道组。

修复后无需手工解除禁用；下一次运行会重新检查该组。

### 数据库身份不匹配

数据库文件会根据频道组名称自动定位。如果保留相同 `name`，却修改了源频道或目标频道，已有数据库中的身份信息会与新配置冲突。

为新频道关系使用新的组名，例如：

```toml
[[channel_groups]]
name = "channel_new"
source_channel = -1001111111111
target_channel = -1009999999999
daily_success_count = 4
```

它会自动使用 `/opt/telegram/TG-Channels-Automated-Operation/data/channel_new.db`。如果确认旧数据库不再需要，应先停止任务并备份，再处理旧文件；不要在程序运行时删除数据库。

### 磁盘空间不足

检查工作目录所在文件系统：

```bash
df -hT /opt/telegram/TG-Channels-Automated-Operation/work
df -i /opt/telegram/TG-Channels-Automated-Operation/work
du -sh /opt/telegram/TG-Channels-Automated-Operation/work
```

检查磁盘和分区：

```bash
lsblk -o NAME,SIZE,TYPE,FSTYPE,FSAVAIL,FSUSE%,MOUNTPOINTS
```

程序检查的是 `work_dir` 实际所在文件系统，而不是 VPS 控制面板显示的整块虚拟磁盘容量。

### 下载速度慢

确认公网基础速度：

```bash
curl -4 -L -o /dev/null -sS \
  -w 'HTTP状态：%{http_code}\n实际下载：%{size_download} bytes\n平均速度：%{speed_download} B/s\n总时间：%{time_total}s\n' \
  'https://fsn1-speed.hetzner.com/100MB.bin'
```

观察容器网络流量：

```bash
docker stats channel-operator
```

查看 `.part` 文件增长：

```bash
watch -n 5 'find work -type f -name "*.part" -exec ls -lh {} \;'
```

调整：

```toml
[runtime]
download_concurrency = 4
```

如果 4 路稳定且没有明显 FloodWait，可以测试 8；如果速度没有明显提升或错误增多，恢复为 4 或 2。

### FloodWait

- 短 FloodWait 会按配置自动等待。
- 日志会显示 `FloodWaitError` 或 `FloodPremiumWaitError`。
- 频繁出现时降低 `download_concurrency`。
- 不要通过同时启动多个脚本绕过限制；全局锁会阻止并行任务。

### CPU 长时间较高

FFmpeg 转码属于计算密集型任务。4 核独享 VPS 使用：

```toml
ffmpeg_threads = 4
```

通常是合理的。如果需要降低温度、功耗或与其他服务共享 CPU，可以改为：

```toml
ffmpeg_threads = 3
```

观察：

```bash
docker stats channel-operator
```

### 当天立即运行却显示已经完成

`daily_success_count` 是每日目标。数据库已经记录该组当天完成数量时，再次运行不会超额发布。

可以：

- 使用一个新的测试频道组和新的测试数据库。
- 等到下一天。
- 临时提高该组的 `daily_success_count`，但要确保不超过 `max_candidates_per_run`。

不要直接编辑生产 SQLite 来绕过去重和恢复逻辑。

### `已有任务正在运行`

说明全局锁已被另一个进程持有。检查：

```bash
docker compose ps
docker top channel-operator
```

不要删除锁文件来强行并行运行。先确认旧任务已经真正结束。

### Docker 挂载或权限错误

检查：

```bash
cd /opt/telegram/TG-Channels-Automated-Operation
docker compose config
ls -ld config.toml .env data work
test -r /opt/telegram/TG-Channels-Automated-Operation/config.toml && echo config-readable
test -r /opt/telegram/TG-Channels-Automated-Operation/.env && echo env-readable
test -w /opt/telegram/TG-Channels-Automated-Operation/data && echo data-writable
test -w /opt/telegram/TG-Channels-Automated-Operation/work && echo work-writable
```

再查看：

```bash
docker compose logs --tail=200 channel-operator
```

## 安全建议

- 不要提交 `.env`、真实 `config.toml`、`.session`、SQLite 数据库和工作目录。
- `.env` 和真实 `config.toml` 建议使用 `root:root` 和 `600`。
- Telethon Session 建议权限为 `600`，所有者为 root。
- 不要在日志、截图、Issue 或聊天中暴露 API Hash、Bot Token 或 Session 文件。
- 项目会关闭 HTTPX/httpcore 的请求 URL 日志，避免 Bot Token 出现在 Bot API URL 日志中。
- 数据库包含目标消息 ID、处理状态和文案，不应公开。
- 更新前备份配置、Session 和数据库。
- 不要同时用多个进程操作同一组数据库。
- 不要在上传过程中强制重启 VPS，除非必须；程序虽然会核对不确定上传，但人工确认仍可能需要。

## 开发与测试

安装开发依赖：

```bash
.venv/bin/python -m pip install -e '.[dev,speed]'
```

运行全部测试：

```bash
.venv/bin/python -m pytest -q
```

运行静态检查：

```bash
.venv/bin/python -m ruff check .
```

检查补丁空白错误：

```bash
git diff --check
```

当前自动测试覆盖：

- 标签过滤、Unicode、HTML 转义和文案截断。
- 真实 FFmpeg 合成视频转码、封面和三张截图。
- SQLite 索引、独立数据库、去重和身份保护。
- 多频道组严格串行顺序。
- 频道封禁跳组、后续组继续和下一次恢复。
- 多路下载、断点续传和失败重试。
- 单次四项媒体组发送、视频元数据和缩略图。
- Bot API 成功、失败、超时、消息拆分和 Token 日志保护。
- CLI 频道组选择和配置校验。

自动测试不会连接真实 Telegram 生产频道。正式上线前仍应使用测试源频道、测试目标频道和全新测试数据库完成端到端验证。
