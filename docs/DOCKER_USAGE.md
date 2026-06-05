# Docker 使用说明

## 快速开始

### 1. 配置环境变量

复制并编辑环境配置文件：

```bash
cp config/env.example config/.env
cp config/symbols.yaml.example config/symbols.yaml  # only needed for multi-bot mode
```

编辑 `config/.env` 文件，设置必要的配置：

```bash
# 交易所配置
EXCHANGE=gate  # 或 binance
CONTRACT_TYPE=USDT  # 合约类型 (仅币安需要)

# API 配置
API_KEY=your_api_key_here
API_SECRET=your_api_secret_here

# 交易配置
COIN_NAME=X
GRID_SPACING=0.004
INITIAL_QUANTITY=1
LEVERAGE=20

# Telegram 通知配置 (可选)
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_CHAT_ID=your_telegram_chat_id_here
ENABLE_NOTIFICATIONS=true
NOTIFICATION_INTERVAL=3600
```

### 2. 构建和运行

#### 使用 Docker Compose (推荐)

```bash
# 构建镜像
docker compose -f docker/docker-compose.yml build

# 运行容器
docker compose -f docker/docker-compose.yml up -d

# 查看日志
docker compose -f docker/docker-compose.yml logs -f

# 停止容器
docker compose -f docker/docker-compose.yml down
```

#### 使用 Docker 命令

```bash
# 构建镜像
docker build -t grid-trading-bot .

# 运行容器
docker run -d \
  --name grid-trader \
  --env-file .env \
  -v $(pwd)/log:/app/log \
  grid-trading-bot

# 查看日志
docker logs -f grid-trader

# 停止容器
docker stop grid-trader
```

## 配置说明

### 交易所选择

- `EXCHANGE=gate`: 运行 Gate.io 版本
- `EXCHANGE=binance`: 运行币安版本

### 合约类型 (仅币安)

- `CONTRACT_TYPE=USDT`: USDT 合约
- `CONTRACT_TYPE=USDC`: USDC 合约

### 环境变量

| 变量名 | 说明 | 默认值 | 必需 |
|--------|------|--------|------|
| EXCHANGE | 交易所选择 | gate | 是 |
| CONTRACT_TYPE | 合约类型 | USDT | 否 |
| API_KEY | API密钥 | - | 是 |
| API_SECRET | API密钥 | - | 是 |
| COIN_NAME | 交易币种 | X | 是 |
| GRID_SPACING | 网格间距 | 0.004 | 是 |
| INITIAL_QUANTITY | 初始数量 | 1 | 是 |
| LEVERAGE | 杠杆倍数 | 20 | 是 |
| TELEGRAM_BOT_TOKEN | Telegram机器人Token | - | 否 |
| TELEGRAM_CHAT_ID | Telegram聊天ID | - | 否 |
| ENABLE_NOTIFICATIONS | 启用通知 | true | 否 |
| NOTIFICATION_INTERVAL | 通知间隔(秒) | 3600 | 否 |

## 日志管理

日志文件会保存在 `./log` 目录中，Docker 容器会自动挂载这个目录。

### 查看实时日志

```bash
# 使用 docker-compose -f docker/docker-compose.yml
docker compose -f docker/docker-compose.yml logs -f

# 使用 docker
docker logs -f grid-trader
```

### 查看历史日志

```bash
# 查看容器日志
docker logs grid-trader

# 查看文件日志
tail -f log/grid_Gate.log
tail -f log/grid_BN.log
```

## 故障排除

### 1. 权限问题

如果遇到权限问题，可以修改用户ID：

```bash
# 在 docker/docker-compose.yml 中修改
USER_ID: 1000
GROUP_ID: 1000
```

### 2. 时区问题

容器内使用 UTC 时区，如果需要本地时区，可以修改 `start.sh` 中的时区设置。

### 3. 网络问题

确保容器能够访问交易所API：

```bash
# 测试网络连接
docker exec grid-trader ping api.gateio.ws
docker exec grid-trader ping fapi.binance.com
```

### 4. 配置验证

启动前会自动验证配置，如果配置错误会显示详细错误信息。

## 安全建议

1. **不要将 `.env` 文件提交到版本控制系统**
2. **定期更新 API 密钥**
3. **使用强密码和双因素认证**
4. **限制 API 权限，只授予必要权限**
5. **监控容器资源使用情况**

## 性能优化

### 资源限制

在 `docker/docker-compose.yml` 中可以调整资源限制：

```yaml
deploy:
  resources:
    limits:
      memory: 512M
      cpus: '0.5'
    reservations:
      memory: 256M
      cpus: '0.25'
```

### 日志轮转

Docker 会自动管理日志文件大小，避免磁盘空间不足。
