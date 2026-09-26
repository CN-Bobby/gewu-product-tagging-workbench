# Amazon Listing 图片采集

工作台新增的 Amazon 图片采集页接受逐行粘贴的 ASIN 或 Amazon 商品链接。完整链接按域名识别站点；裸 ASIN 使用页面选择的站点，默认美国站。重复项目会按站点和 ASIN 去重。首版只采集 Listing 图，不包含 A+ 或 Brand Story。

## 访问节奏与代理

- 每组最多提交 100 个去重后的商品；超过时会明确报错，不会自动截掉后面的商品。输入文本最多 128 KiB，相关接口请求体最多 256 KiB。
- 任务组按提交顺序排队，同时最多运行 2 组。所有任务共享最多 4 个页面名额；空出名额后按顺序轮流补位。取消等待中的任务会直接从队列移除。
- `stable` 每组最多使用 1 个页面，页面间隔 6–10 秒；`balanced` 最多 2 个，间隔 2–5 秒；`fast` 最多 4 个，间隔 1–2 秒。档位是单组上限，所有组仍共享总计 4 个页面名额。默认 `balanced`。间隔按实际开始请求的时间计算。
- 发现 CAPTCHA、429 或 503 后停止该组新增请求、降为单页面并冷却 30 秒；前两次受限后冷却，第三次连续受限后暂停任务。成功采集一个商品会清零连续受限次数。系统保留已完成图片和进度，用户可稍后继续。
- 不自动求解 CAPTCHA，不保存浏览器 Cookie/Profile。仅访问 HTTPS Amazon 商品域名和 Amazon 图片 CDN；图片限制为 JPEG/PNG、单张最多 15 MB、每商品最多 10 张。
- 可把代理 URL 按行放在数据目录 `amazon-scraper/proxies.txt`，以 `#` 开头的行为注释。也可通过进程环境变量 `AMAZON_SCRAPER_PROXY` 指定一个代理，或用 `AMAZON_SCRAPER_PROXY_FILE` 指定代理池文件。代理凭据只在当前运行时读取，不写入任务状态、日志或工作簿。

## 浏览器运行环境

系统 Edge/Chrome 可直接使用。没有可用系统浏览器时，手动运行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/prepare-amazon-scraper-runtime.ps1
```

安装脚本只在用户明确启动时下载 Playwright Chromium；工作台运行时仅检查是否可用。

## 输出与恢复

成功商品写入一个可导入的 `.xlsx` 工作簿：每个商品一行，含 ASIN、标题和图片列；首张图成为格物主图，余图成为补充图。导入后创建新的格物工作表。失败项、CAPTCHA 和访问受限状态显示在任务面板；重启后未完成的项目保留为可继续任务。

默认保存到当前数据目录的 `amazon-scraper/<job-id>/`，不写入仓库。任务记录不保留原始输入中的查询参数或代理凭据。
