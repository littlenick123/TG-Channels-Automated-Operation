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
- [快速安装](#快速安装)
- [.env 完整说明](#env-完整说明)
- [config.toml 完整说明](#configtoml-完整说明)
- [五个目标频道的完整配置示例](#五个目标频道的完整配置示例)
- [命令行完整说明](#命令行完整说明)
- [首次运行顺序](#首次运行顺序)
- [多频道串行、数据库和失败处理](#多频道串行数据库和失败处理)
- [systemd 生产部署](#systemd-生产部署)
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
- Python 3.12。
- FFmpeg 和 FFprobe 6 或更高版本。
- 4 核 CPU、6 GiB 或更多内存。
- 足够容纳源视频、转码视频和临时文件的磁盘空间。
- 能稳定访问 Telegram MTProto 和 `api.telegram.org` 的网络。
- 一个已经加入所有源频道的 Telegram 用户账号。
- 该用户账号在所有目标频道中具有发帖权限。
- 从 [my.telegram.org](https://my.telegram.org/) 获取的 `api_id` 和 `api_hash`。
- 一个由 BotFather 创建的报告机器人。

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

这个正整数就是私人 `chat_id`。把它写入 `config.toml` 的 `[reporting] chat_id`。不要公开完整的 `getUpdates` 输出，因为其中可能包含私人消息和账号信息。

### 4. 准备频道权限

- 用户账号必须已经加入每一个源私密频道。
- 用户账号必须可以读取源频道历史。
- 用户账号必须是目标频道管理员，且具有发布消息权限。
- 报告机器人不需要加入源频道或目标频道。
- 频道 ID 通常是以 `-100` 开头的负整数，也可以使用公开频道用户名。

## 快速安装

以下命令适合已经把项目放到 VPS 某个目录后手工测试。进入实际项目目录，例如：

```bash
cd /opt/telegram/TG-Channels-Automated-Operation
```

安装系统依赖：

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv ffmpeg git curl
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

`.env` 必须与传给 `--config` 的配置文件位于同一个目录。例如使用 `--config /etc/channel-operator/config.toml` 时，程序读取 `/etc/channel-operator/.env`。

完整示例：

```dotenv
TG_API_ID=123456
TG_API_HASH=0123456789abcdef0123456789abcdef
TG_PHONE=+8613800000000
TG_SESSION_PATH=/var/lib/channel-operator/telegram-user
TG_REPORT_BOT_TOKEN=123456:ABCDEF_replace_me
```

字段说明：

| 变量 | 必填 | 示例 | 说明 |
|---|---|---|---|
| `TG_API_ID` | 是 | `123456` | 从 my.telegram.org 获取的整数 API ID。 |
| `TG_API_HASH` | 是 | `0123...cdef` | 从 my.telegram.org 获取的 API Hash，属于敏感凭据。 |
| `TG_PHONE` | 登录时建议 | `+8613800000000` | Telegram 用户账号手机号，使用带国家区号的 E.164 格式。 |
| `TG_SESSION_PATH` | 建议显式设置 | `/var/lib/channel-operator/telegram-user` | Telethon 用户会话路径；省略时默认使用配置文件目录下的 `./data/telegram-user`。通常不要手工添加 `.session` 后缀，父目录必须可写。 |
| `TG_REPORT_BOT_TOKEN` | 是 | `123456:ABC...` | BotFather 提供的报告机器人 Token。 |

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
- 这个字段用于记录预期运行时间，但程序自身不会常驻并等待该时间。
- 真正的自动触发时间由 systemd timer 的 `OnCalendar` 决定。
- 修改运行时间时，必须同时修改 `daily_time` 和 timer 的 `OnCalendar`。

### `[reporting]` 机器人报告

```toml
[reporting]
chat_id = 123456789
```

#### `chat_id`

- 接收报告的私人 Telegram chat ID。
- 当前配置要求是正整数。
- 你必须先向报告机器人发送 `/start`，否则机器人无法主动给你发送消息。
- `doctor` 会调用 Bot API 验证 Token，并真实发送一条测试消息。

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
database_dir = "/var/lib/channel-operator"
work_dir = "/var/cache/channel-operator/work"
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
- 例如 `database_dir = "/var/lib/channel-operator"` 且组名为 `channel_b`，实际数据库为 `/var/lib/channel-operator/channel_b.db`。
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
channel_b → /var/lib/channel-operator/channel_b.db
channel_c → /var/lib/channel-operator/channel_c.db
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
chat_id = 123456789

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
database_dir = "/var/lib/channel-operator"
work_dir = "/var/cache/channel-operator/work"
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
/opt/telegram-channel-operator/.venv/bin/channel-operator \
  --config /etc/channel-operator/config.toml doctor
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
- 报告机器人能否向私人 `chat_id` 发送消息。
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

### 1. 检查配置语法但暂不启动 systemd

确保 `.env` 和 `config.toml` 已填写，然后登录：

```bash
.venv/bin/channel-operator --config config.toml login
```

### 2. 执行 doctor

```bash
.venv/bin/channel-operator --config config.toml doctor
```

确认：

- FFmpeg/FFprobe 输出正常。
- 每个频道组的 `post_permission` 为 `ok`。
- 私人 Telegram 会话收到机器人测试消息。
- 每个频道组显示的自动数据库路径正确。

### 3. 建立索引

```bash
.venv/bin/channel-operator --config config.toml index
```

首次索引可能需要较长时间。各频道组数据库独立，因此相同源频道会分别扫描和保存状态。

### 4. 预览选材和文案

```bash
.venv/bin/channel-operator --config config.toml run-once --dry-run
```

也可以先只测试一个组：

```bash
.venv/bin/channel-operator --config config.toml run-once --dry-run --group channel_b
```

### 5. 在测试频道正式发布

建议先把目标 ID 配置为测试频道，并为测试关系使用一个全新的频道组名称，使程序自动创建新的测试数据库：

```bash
.venv/bin/channel-operator --config config.toml run-once --group channel_b
```

确认 Telegram 中满足：

- 一个视频加三张图片显示为一个媒体组。
- 视频位于第一项。
- 文案只在视频上。
- 视频封面和比例正确。
- 视频能够流式播放。
- 数据库和工作目录没有异常。

验证完成后再启用 systemd 定时运行。

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

## systemd 生产部署

下面使用统一生产路径：

```text
项目：/opt/telegram-channel-operator
配置：/etc/channel-operator/config.toml
环境：/etc/channel-operator/.env
会话和数据库：/var/lib/channel-operator
工作目录：/var/cache/channel-operator/work
```

如果你的项目实际位于 `/opt/telegram/TG-Channels-Automated-Operation`，可以继续使用该路径，但必须同步修改 systemd service 的 `WorkingDirectory` 和 `ExecStart`。

### 1. 创建专用系统用户和目录

```bash
sudo useradd --system --home /var/lib/channel-operator --create-home channel-operator
sudo mkdir -p /opt/telegram-channel-operator
sudo mkdir -p /etc/channel-operator
sudo mkdir -p /var/cache/channel-operator/work
sudo chown -R channel-operator:channel-operator /opt/telegram-channel-operator
sudo chown -R channel-operator:channel-operator /var/lib/channel-operator
sudo chown -R channel-operator:channel-operator /var/cache/channel-operator
```

如果用户已经存在，`useradd` 提示存在可以忽略，不要重复删除用户。

### 2. 获取项目源码

首次从 Git 仓库部署：

```bash
sudo -u channel-operator git clone YOUR_REPOSITORY_URL /opt/telegram-channel-operator
cd /opt/telegram-channel-operator
```

如果源码已经在该目录，只需要进入目录：

```bash
cd /opt/telegram-channel-operator
```

### 3. 创建生产虚拟环境

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv ffmpeg git curl

sudo -u channel-operator python3.12 -m venv /opt/telegram-channel-operator/.venv
sudo -u channel-operator /opt/telegram-channel-operator/.venv/bin/python \
  -m pip install --upgrade pip
sudo -u channel-operator /opt/telegram-channel-operator/.venv/bin/python \
  -m pip install -e '/opt/telegram-channel-operator[speed]'
```

也可以在项目目录使用：

```bash
sudo -u channel-operator /opt/telegram-channel-operator/.venv/bin/python \
  -m pip install -e '.[speed]'
```

### 4. 安装生产配置

```bash
sudo cp /opt/telegram-channel-operator/deploy/config.production.toml \
  /etc/channel-operator/config.toml
sudo cp /opt/telegram-channel-operator/.env.example \
  /etc/channel-operator/.env
sudo chown root:channel-operator \
  /etc/channel-operator/config.toml \
  /etc/channel-operator/.env
sudo chmod 640 \
  /etc/channel-operator/config.toml \
  /etc/channel-operator/.env
```

编辑真实配置：

```bash
sudo nano /etc/channel-operator/.env
sudo nano /etc/channel-operator/config.toml
```

生产 `.env` 至少应包含：

```dotenv
TG_API_ID=123456
TG_API_HASH=替换为真实API_HASH
TG_PHONE=+8613800000000
TG_SESSION_PATH=/var/lib/channel-operator/telegram-user
TG_REPORT_BOT_TOKEN=替换为真实机器人Token
```

生产 TOML 中建议使用：

```toml
[runtime]
database_dir = "/var/lib/channel-operator"
work_dir = "/var/cache/channel-operator/work"

[[channel_groups]]
name = "channel_b"
# 其余字段按实际填写
```

每新增一个频道组，只需要使用新的唯一 `name`。程序会自动创建 `/var/lib/channel-operator/<name>.db`，不需要也不允许在频道组中填写 `database_path`。

### 5. 登录 Telethon 用户账号

```bash
sudo -u channel-operator /opt/telegram-channel-operator/.venv/bin/channel-operator \
  --config /etc/channel-operator/config.toml login
```

确认会话文件：

```bash
sudo ls -lh /var/lib/channel-operator/telegram-user.session
```

建议权限：

```bash
sudo chown channel-operator:channel-operator \
  /var/lib/channel-operator/telegram-user.session
sudo chmod 600 /var/lib/channel-operator/telegram-user.session
```

### 6. 运行生产 doctor

```bash
sudo -u channel-operator /opt/telegram-channel-operator/.venv/bin/channel-operator \
  --config /etc/channel-operator/config.toml doctor
```

此时私人 Telegram 应收到机器人测试消息。任何频道组显示 `ERROR` 时，不要启用定时任务，先修复频道 ID、权限或 `database_dir` 权限。

### 7. 建立生产索引和预览

```bash
sudo -u channel-operator /opt/telegram-channel-operator/.venv/bin/channel-operator \
  --config /etc/channel-operator/config.toml index

sudo -u channel-operator /opt/telegram-channel-operator/.venv/bin/channel-operator \
  --config /etc/channel-operator/config.toml run-once --dry-run
```

### 8. 安装 systemd 文件

```bash
sudo cp /opt/telegram-channel-operator/deploy/systemd/channel-operator.service \
  /etc/systemd/system/channel-operator.service
sudo cp /opt/telegram-channel-operator/deploy/systemd/channel-operator.timer \
  /etc/systemd/system/channel-operator.timer
```

检查 service 中路径是否与实际部署一致：

```bash
sudo systemctl cat channel-operator.service
```

项目提供的 service 使用：

```ini
WorkingDirectory=/opt/telegram-channel-operator
ExecStart=/opt/telegram-channel-operator/.venv/bin/channel-operator --config /etc/channel-operator/config.toml run-once
```

如果项目目录不同，编辑 service：

```bash
sudo systemctl edit --full channel-operator.service
```

### 9. 检查和修改定时器

默认每天北京时间 00:01 运行：

```ini
OnCalendar=*-*-* 00:01:00 Asia/Shanghai
Persistent=true
```

修改时间：

```bash
sudo systemctl edit --full channel-operator.timer
```

例如每天北京时间 03:30：

```ini
OnCalendar=*-*-* 03:30:00 Asia/Shanghai
```

同时把 `config.toml` 改为：

```toml
[schedule]
timezone = "Asia/Shanghai"
daily_time = "03:30"
```

### 10. 启用定时任务

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now channel-operator.timer
systemctl list-timers channel-operator.timer
```

手工触发一次 systemd service：

```bash
sudo systemctl start channel-operator.service
systemctl status channel-operator.service --no-pager
```

查看实时日志：

```bash
journalctl -u channel-operator.service -f
```

查看最近 300 行：

```bash
journalctl -u channel-operator.service -n 300 --no-pager
```

查看指定日期日志：

```bash
journalctl -u channel-operator.service \
  --since "2026-08-09 00:00:00" \
  --until "2026-08-10 00:00:00" \
  --no-pager
```

### systemd 安全限制

提供的 service 使用：

- `NoNewPrivileges=true`
- `PrivateTmp=true`
- `ProtectSystem=strict`
- `ProtectHome=true`
- 只允许写入 `/var/lib/channel-operator` 和 `/var/cache/channel-operator`

因此生产数据库、Session 和工作目录必须放在允许写入的路径中。如果把数据库配置到 `/opt` 或 `/etc`，systemd 运行时可能出现权限错误。

## 更新源码和升级

### 1. 在更新前确认任务没有运行

```bash
systemctl is-active channel-operator.service
```

如果输出 `active`，建议等待当前媒体组和任务自然完成。不要在上传过程中强制更新。

临时停止后续定时触发：

```bash
sudo systemctl stop channel-operator.timer
```

### 2. 备份配置、会话和数据库

```bash
sudo mkdir -p /var/backups/channel-operator
sudo cp -a /etc/channel-operator \
  /var/backups/channel-operator/etc-channel-operator
sudo cp -a /var/lib/channel-operator \
  /var/backups/channel-operator/var-lib-channel-operator
```

如果目标备份目录已经存在，先为备份目录增加日期后缀，避免覆盖旧备份。

### 3. 拉取源码

```bash
cd /opt/telegram-channel-operator
sudo -u channel-operator git status --short --branch
sudo -u channel-operator git pull --ff-only
```

如果显示 `Already up to date.`，说明远程仓库没有比 VPS 更新的提交。必须先在开发机器提交并推送本地修改。

### 4. 重新安装依赖

即使只是拉取 Python 源码，也建议执行：

```bash
sudo -u channel-operator /opt/telegram-channel-operator/.venv/bin/python \
  -m pip install -e '.[speed]'
```

这一步会安装新增依赖，例如报告机器人使用的 HTTPX。

### 5. 检查配置兼容性

当前多频道版本不支持旧配置：

```toml
[telegram]
source_channel = ...
target_channel = ...
```

也不再从以下位置读取单一数据库和每日数量：

```toml
[schedule]
daily_success_count = 4

[runtime]
database_path = "./data/operator.db"
```

必须改用一个或多个 `[[channel_groups]]`。新版数据库目录只在 `[runtime]` 中统一配置：

```toml
[runtime]
database_dir = "/var/lib/channel-operator"

[[channel_groups]]
name = "channel_b"
source_channel = -1001111111111
target_channel = -1002222222222
daily_success_count = 4
```

如果升级前已经在每个 `[[channel_groups]]` 中配置了 `database_path`，请删除这些行，并确保旧数据库文件名正好是 `<name>.db` 后放入 `database_dir`。例如 `channel_b` 对应 `channel_b.db`。文件名符合规则时可以继续使用原数据库；不要在程序运行时移动数据库及其 `-wal`、`-shm` 文件。

### 6. 验证并重新启用

```bash
sudo -u channel-operator /opt/telegram-channel-operator/.venv/bin/channel-operator \
  --config /etc/channel-operator/config.toml doctor

sudo -u channel-operator /opt/telegram-channel-operator/.venv/bin/channel-operator \
  --config /etc/channel-operator/config.toml run-once --dry-run

sudo systemctl daemon-reload
sudo systemctl restart channel-operator.timer
systemctl list-timers channel-operator.timer
```

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

### 报告机器人提示 `chat not found` 或收不到消息

- 先打开机器人并发送 `/start`。
- 确认 `[reporting] chat_id` 是你的私人正整数 ID，不是频道 ID。
- 再次调用 `getUpdates` 检查 ID。
- 运行 `doctor`，它会真实发送测试消息。

### `Telethon 会话尚未登录`

执行：

```bash
.venv/bin/channel-operator --config config.toml login
```

systemd 部署时必须使用与 service 相同的 `channel-operator` 用户登录，否则 Session 可能生成在错误位置或权限不正确。

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

它会自动使用 `/var/lib/channel-operator/channel_new.db`。如果确认旧数据库不再需要，应先停止任务并备份，再处理旧文件；不要在程序运行时删除数据库。

### 磁盘空间不足

检查工作目录所在文件系统：

```bash
df -hT /var/cache/channel-operator/work
df -i /var/cache/channel-operator/work
du -sh /var/cache/channel-operator/work
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

观察 Telegram 媒体连接：

```bash
ss -tinp | grep ':443'
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
top
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
ps aux | grep channel-operator
systemctl status channel-operator.service --no-pager
```

不要删除锁文件来强行并行运行。先确认旧任务已经真正结束。

### systemd 权限错误

检查：

```bash
sudo -u channel-operator test -r /etc/channel-operator/config.toml && echo config-readable
sudo -u channel-operator test -r /etc/channel-operator/.env && echo env-readable
sudo -u channel-operator test -w /var/lib/channel-operator && echo data-writable
sudo -u channel-operator test -w /var/cache/channel-operator/work && echo work-writable
```

再查看：

```bash
journalctl -u channel-operator.service -n 200 --no-pager
```

## 安全建议

- 不要提交 `.env`、真实 `config.toml`、`.session`、SQLite 数据库和工作目录。
- `.env` 建议权限为 `600`；systemd 场景可用 `root:channel-operator` 和 `640`。
- Telethon Session 建议权限为 `600`，所有者为运行服务的用户。
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
