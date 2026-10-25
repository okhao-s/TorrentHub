# TorrentHub

qBittorrent / Transmission 多实例统一面板：看数据 + 控制。单容器，SQLite 存实例配置与设置，种子数据全部实时读。

## 功能

- **多实例聚合**：按 info hash 跨实例合并同一资源，标注双份做种
- **浏览**：搜索 / 状态筛选 / 实例筛选 / 标签筛选 / 排序 / 分页
- **控制**：暂停 / 恢复 / 重新校验 / 重新汇报 / 删除（可选连文件）
- **重试错误**：全部或按 tracker 域名分组重试，避免一次性轰所有 tracker
- **添加任务**：磁力/URL 多行、`.torrent` 多文件上传、保存路径、分类/标签、暂停启动；支持按名称正则路由到不同实例
- **限速**：实例全局上下行限速、单任务限速
- **详情抽屉**：保存路径、tracker 状态、文件列表、peers、限速
- **批量修改**：分类（qB）/标签（qB + TR）
- **磁盘水位**：各实例剩余空间，同卷实例标注
- **趋势**：每 5 分钟采样，顶栏 24h 上传速度迷你曲线
- **审计日志**：删除/暂停/限速/添加等操作落库
- **访问密码**：HMAC 签名的 HttpOnly cookie
- **移动端**：窄屏自动切换卡片视图

## 运行

```bash
docker run -d --name TorrentHub --restart unless-stopped \
  -p 9162:8080 -v /root/torrent-hub/data:/app/data \
  -e TZ=Asia/Shanghai -e TH_VERSION=20261025i \
  okhao/torrenthub:latest
```

环境变量：`TH_DB_PATH`（默认 `/app/data/hub.db`）、`TH_VERSION`（版本号显示）。

## 本地开发

```bash
TH_DB_PATH=/root/torrent-hub/dev.db .venv/bin/uvicorn app:app --host 127.0.0.1 --port 19164
```

## 说明

- 不写入任何下载器配置，只调用各自 Web API。
- TR 没有分类概念，分类在 TR 侧会作为标签写入。
- 单实例限速与下载器全局限速同源，改动会直接影响该实例。
