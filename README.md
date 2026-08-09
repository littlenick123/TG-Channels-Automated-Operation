# Telegram 频道自动运营

这个项目使用 Telethon 用户会话读取私密源频道，将单视频媒体组中的 1080P 视频下载并转码为 720P，生成三张截图，然后通过一次 Telegram 相册调用发布到目标频道。

目标媒体组固定为：

1. `video.mp4`
2. `frame_1.jpg`
3. `frame_2.jpg`
4. `frame_3.jpg`

四个媒体项共享同一个 Telegram `grouped_id`，在客户端中显示为一个媒体组。文案只附在第一个视频上。

## 运行条件

- Ubuntu 24.04 或其他已经提供 Python 3.12 的兼容 Linux
- Python 3.12
- FFmpeg/ffprobe 6 或更高版本
- 能访问 Telegram 的网络
- 用户账号已经加入源私密频道，并具有目标频道发帖权限
- Telegram `api_id` 和 `api_hash`，可从 [my.telegram.org](https://my.telegram.org/) 获取

建议 VPS 至少保留可容纳“原视频大小 × 3 + 1 GiB”的空闲磁盘。程序一次只处理一个媒体组，FFmpeg 默认最多使用 3 个线程。

## 本地安装

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv ffmpeg

python3.12 -m venv .venv
.venv/bin/pip install -e '.[speed]'
cp config.example.toml config.toml
cp .env.example .env
```

修改 `.env`：

```dotenv
TG_API_ID=123456
TG_API_HASH=你的_api_hash
TG_PHONE=+8613800000000
TG_SESSION_PATH=./data/telegram-user
```

修改 `config.toml` 中的：

- `source_channel`：私密源频道 ID 或用户名
- `target_channel`：目标频道 ID 或用户名
- `keep_tags`：优先保留的标签列表
- `drop_tags`：必须删除的标签列表

频道数字 ID 通常以 `-100` 开头。`keep_tags` 可以配置几十个或更多标签，作为优先保留标签库；它与 `drop_tags` 不能重叠。单篇文案仍最多输出 5 个标签：按配置顺序优先采用原文实际出现的 `keep_tags`，不足 5 个时再从普通标签中随机补足。

## 首次启动

先登录用户账号：

```bash
.venv/bin/channel-operator --config config.toml login
```

终端会要求输入 Telegram 验证码；账号开启两步验证时还会要求密码。生成的 `.session` 文件等同登录凭据，必须限制访问权限并纳入备份保护，不能提交到 Git。

然后依次执行：

```bash
.venv/bin/channel-operator --config config.toml doctor
.venv/bin/channel-operator --config config.toml index
.venv/bin/channel-operator --config config.toml run-once --dry-run
.venv/bin/channel-operator --config config.toml run-once
```

- `doctor` 检查 FFmpeg、会话、频道访问权和目标发帖权。
- `index` 首次扫描完整历史，以后从 SQLite 检查点增量扫描。
- `run-once --dry-run` 随机预览候选组和处理后的文案，不下载、不转码、不发布，也不占用候选组。
- `run-once` 以当天成功发布 4 组为目标。

## 选择、文案与失败处理

- 只选择带 `grouped_id`、恰好一个视频且视频短边至少 1080 像素的媒体组。
- 成功发布的源组永久排除，不会跨天复用。
- 标签每次重新处理时重新随机；同一次上传的网络重试使用同一份已保存文案。
- 标签最多保留 5 个，素材不足时允许少于 5 个。
- 读取以 `#` 开头的标签行，也支持 `标签：#标签` 和 `标签: #标签` 格式；`@用户名` 不作为标签处理。简介只读取 `简介：` 或 `简介:` 当前行。
- 标签过滤后没有任何标签、同时也没有有效简介时，媒体组会在下载前被永久跳过，并自动选择新的候选补足当天发布数量；`--dry-run` 同样会过滤空文案候选。
- 长视频截图时间为 20 秒、中点和结束前 60 秒；不超过 120 秒时改用 10%、50%、90%。
- 视频封面从转码成品的第 0 帧单独生成，不复用三张内容截图；封面会缩放为最长边不超过 320 像素、文件不超过 20 KiB 的 JPEG。
- 上传视频时显式携带 FFprobe 得到的宽度、高度和时长，并通过 `hachoir` 提供媒体元数据兼容支持，避免 Telegram 按错误比例渲染未播放预览。
- 单个视频默认使用 4 路交错分片并发下载，每路分片为 512 KiB；可通过 `[runtime] download_concurrency` 在 1 到 8 之间调整。媒体组之间仍然串行处理，不会同时转码或发布多个媒体组。
- 60 秒以内的 FloodWait 默认由 Telethon 在当前请求内自动等待；更长等待和网络错误会保留 `.part` 临时文件，并从已完成的 512 KiB 边界续传，不会重新从第 0 字节下载。
- 日志会显示 `FloodWaitError` 或 `FloodPremiumWaitError` 的真实类型，便于区分普通频率限制与非会员下载限速；自动等待阈值可通过 `[runtime] flood_sleep_threshold_seconds` 调整。
- 上传响应丢失时，程序先到目标频道核对相册，再决定是否重试，减少重复发帖风险。
- 每次结束会把成功数和错误摘要发送到当前账号的“收藏夹”。

状态数据库默认位于 `./data/operator.db`。不要在任务运行时手工修改数据库。

## systemd 部署

建议使用专用系统用户：

```bash
sudo useradd --system --home /var/lib/channel-operator --create-home channel-operator
sudo mkdir -p /opt/telegram-channel-operator /etc/channel-operator /var/cache/channel-operator
sudo chown -R channel-operator:channel-operator /opt/telegram-channel-operator /var/lib/channel-operator /var/cache/channel-operator
```

将项目复制到 `/opt/telegram-channel-operator`，在该目录创建虚拟环境并安装项目。然后：

```bash
sudo cp deploy/config.production.toml /etc/channel-operator/config.toml
sudo cp .env.example /etc/channel-operator/.env
sudo cp deploy/systemd/channel-operator.service /etc/systemd/system/
sudo cp deploy/systemd/channel-operator.timer /etc/systemd/system/
sudo chown root:channel-operator /etc/channel-operator/config.toml /etc/channel-operator/.env
sudo chmod 640 /etc/channel-operator/config.toml /etc/channel-operator/.env
```

把生产配置中的频道、标签、API 凭据和会话路径改为真实值，推荐：

```dotenv
TG_SESSION_PATH=/var/lib/channel-operator/telegram-user
```

首次登录需要用专用用户执行：

```bash
sudo -u channel-operator /opt/telegram-channel-operator/.venv/bin/channel-operator \
  --config /etc/channel-operator/config.toml login
```

验证并启用每天北京时间 `00:01` 的定时器：

```bash
sudo -u channel-operator /opt/telegram-channel-operator/.venv/bin/channel-operator \
  --config /etc/channel-operator/config.toml doctor
sudo -u channel-operator /opt/telegram-channel-operator/.venv/bin/channel-operator \
  --config /etc/channel-operator/config.toml index
sudo systemctl daemon-reload
sudo systemctl enable --now channel-operator.timer
systemctl list-timers channel-operator.timer
```

查看运行状态：

```bash
systemctl status channel-operator.timer
journalctl -u channel-operator.service -n 200 --no-pager
```

如果修改每日运行时间，需要同时修改 `config.toml` 的 `daily_time` 和 `channel-operator.timer` 的 `OnCalendar`，然后执行 `sudo systemctl daemon-reload && sudo systemctl restart channel-operator.timer`。

## 开发与测试

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check src tests
.venv/bin/pytest -q
```

测试包含真实 FFmpeg 合成视频转码、截图时间规则、SQLite 去重恢复、随机标签和单次四项媒体组发送验证。真实 Telegram 端到端测试需要由使用者提供测试频道和账号，因此不会在自动测试中连接生产频道。
