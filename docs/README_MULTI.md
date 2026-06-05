# 多币种网格交易机器人 - 完整运行说明

## 概述

本项目是币安永续合约网格交易机器人的多币种版本，支持同时运行多个币种的网格交易策略。所有网格策略逻辑与单币种版本完全一致，只是增加了多币种并行运行的支持。

## 文件结构

```
grid/
├── src/multi_bot/binance_multi_bot.py # BinanceGridBot 类实现
├── src/single_bot/binance_bot.py      # 单币种入口文件
├── src/multi_bot/multi_bot.py         # 多币种入口文件
├── symbols.yaml            # 多币种配置文件
├── symbols.json            # JSON格式配置文件
├── scripts/deploy.sh       # 部署脚本
├── docker/docker-compose.yml # Docker配置
├── health_check.py         # 健康检查脚本
├── scripts/start.sh         # 启动脚本
├── config/.env             # 环境变量配置
└── log/                    # 日志目录
    ├── multi_grid_BN.log   # 主日志
    ├── status_summary.log  # 状态汇总日志
    └── grid_BN_*.log       # 各币种日志
```

## 快速开始

### 1. 环境准备

```bash
# 安装依赖
pip install ccxt websockets python-dotenv pyyaml aiohttp

# 配置环境变量
cp config/env.example config/.env
# 编辑 config/.env 文件，设置 API 密钥等信息
```

### 2. 配置文件设置

创建 `symbols.yaml` 文件：

```yaml
symbols:
  - name: BTCUSDT
    grid_spacing: 0.004
    initial_quantity: 0.001
    leverage: 20
    contract_type: USDT
    
  - name: ETHUSDT
    grid_spacing: 0.005
    initial_quantity: 0.01
    leverage: 20
    contract_type: USDT
```

### 3. 启动方式

#### 方式一：直接运行
```bash
# 启动单币种模式
python3 src/single_bot/binance_bot.py

# 启动多币种模式
python3 src/multi_bot/multi_bot.py

# 或使用启动脚本
./scripts/start.sh single    # 单币种
./scripts/start.sh multi     # 多币种
```

#### 方式二：Docker 运行
```bash
# 构建镜像
./scripts/deploy.sh build

# 启动单币种模式
./scripts/deploy.sh start

# 启动多币种模式
./scripts/deploy.sh multi-start
```

## 详细配置说明

### 环境变量配置 (.env)

```bash
# 必填配置
API_KEY=your_binance_api_key
API_SECRET=your_binance_api_secret

# 可选配置
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id
ENABLE_NOTIFICATIONS=true
NOTIFICATION_INTERVAL=3600
```

### 多币种配置 (symbols.yaml)

```yaml
symbols:
  - name: BTCUSDT              # 交易对名称
    grid_spacing: 0.004        # 网格间距 (0.001-0.01)
    initial_quantity: 0.001    # 初始交易数量
    leverage: 20               # 杠杆倍数 (1-100)
    contract_type: USDT        # 合约类型 (USDT/USDC)
    
  - name: ETHUSDT
    grid_spacing: 0.005
    initial_quantity: 0.01
    leverage: 20
    contract_type: USDT
```

### 配置参数说明

| 参数 | 说明 | 推荐范围 | 示例 |
|------|------|----------|------|
| `name` | 交易对名称 | 币安支持的永续合约 | BTCUSDT, ETHUSDT |
| `grid_spacing` | 网格间距 | 0.001-0.01 | 0.004 (0.4%) |
| `initial_quantity` | 初始数量 | 根据币种价格调整 | BTC: 0.001, ETH: 0.01 |
| `leverage` | 杠杆倍数 | 1-100 | 20 |
| `contract_type` | 合约类型 | USDT/USDC | USDT |

## 日志管理

### 日志文件说明

- `log/multi_grid_BN.log`: 主控制日志
- `log/status_summary.log`: 状态汇总日志
- `log/grid_BN_BTCUSDT.log`: BTC 币种日志
- `log/grid_BN_ETHUSDT.log`: ETH 币种日志

### 查看日志

```bash
# 查看主日志
tail -f log/multi_grid_BN.log

# 查看状态汇总
tail -f log/status_summary.log

# 查看特定币种日志
tail -f log/grid_BN_BTCUSDT.log

# 使用部署脚本查看
./scripts/deploy.sh multi-logs    # 查看汇总日志
./scripts/deploy.sh bot-logs      # 查看币种日志
```

### 日志轮转

- 按日期自动分割：每天午夜创建新文件
- 保留时间：最近7天的日志文件
- 文件命名：`grid_BN_BTCUSDT.log.2024-01-15`

## 健康检查

### 手动检查
```bash
python3 health_check.py
```

### Docker 健康检查
```bash
# 查看容器健康状态
docker inspect grid-trader --format='{{.State.Health.Status}}'

# 查看健康检查日志
docker inspect grid-trader --format='{{.State.Health.Log}}'
```

### 健康检查项目

1. **状态汇总日志**: 检查是否正常更新
2. **主日志文件**: 检查文件大小和错误
3. **币种日志文件**: 检查各币种运行状态
4. **进程状态**: 检查主进程是否存活

## 部署脚本使用

### 基本命令

```bash
./scripts/deploy.sh build          # 构建 Docker 镜像
./scripts/deploy.sh start          # 启动单币种模式
./scripts/deploy.sh multi-start    # 启动多币种模式
./scripts/deploy.sh stop           # 停止服务
./scripts/deploy.sh restart        # 重启服务
./scripts/deploy.sh logs           # 查看容器日志
./scripts/deploy.sh multi-logs     # 查看汇总日志
./scripts/deploy.sh bot-logs       # 查看币种日志
./scripts/deploy.sh status         # 查看状态
./scripts/deploy.sh cleanup        # 清理资源
```

### Docker 管理

```bash
# 查看容器状态
docker-compose -f docker/docker-compose.yml ps

# 查看资源使用
docker stats grid-trader

# 进入容器
docker exec -it grid-trader bash

# 查看容器日志
docker-compose -f docker/docker-compose.yml logs -f
```

## 故障排查

### 常见问题

1. **API 密钥错误**
   ```bash
   # 检查环境变量
   docker exec grid-trader env | grep API
   ```

2. **配置文件错误**
   ```bash
   # 验证 YAML 格式
   python3 -c "import yaml; yaml.safe_load(open('symbols.yaml'))"
   ```

3. **网络连接问题**
   ```bash
   # 检查网络连接
   docker exec grid-trader ping -c 3 fstream.binance.com
   ```

4. **日志文件权限**
   ```bash
   # 修复权限
   sudo chown -R $USER:$USER log/
   chmod 755 log/
   ```

### 重启服务

```bash
# 完全重启
./scripts/deploy.sh stop
./scripts/deploy.sh multi-start

# 重新构建
./scripts/deploy.sh build
./scripts/deploy.sh multi-start
```

## 性能监控

### 资源使用监控

```bash
# 查看容器资源使用
docker stats grid-trader

# 查看日志文件大小
du -sh log/*.log

# 查看磁盘使用
df -h
```

### 状态监控

```bash
# 查看活跃机器人
tail -1 log/status_summary.log

# 查看错误日志
grep ERROR log/multi_grid_BN.log

# 查看启动状态
grep "启动成功" log/multi_grid_BN.log
```

## 安全注意事项

1. **API 密钥安全**
   - 不要在代码中硬编码 API 密钥
   - 使用环境变量或 .env 文件
   - 定期更换 API 密钥

2. **权限控制**
   - 限制 API 密钥权限（只读 + 交易）
   - 设置 IP 白名单
   - 启用双因素认证

3. **资金安全**
   - 使用测试网络进行测试
   - 从小额开始测试
   - 设置合理的止损

## 版本兼容性

### 向后兼容

- 单币种版本 `src/single_bot/binance_bot.py` 完全兼容
- 原有的 `.env` 配置可以直接使用
- 原有的日志格式保持不变

### 升级路径

1. **从单币种升级到多币种**
   ```bash
   # 备份原有配置
   cp .env .env.backup
   
   # 创建多币种配置
   cp symbols.yaml.example symbols.yaml
   # 编辑 symbols.yaml
   
   # 启动多币种模式
   ./scripts/deploy.sh multi-start
   ```

2. **回退到单币种**
   ```bash
   # 停止多币种服务
   ./scripts/deploy.sh stop
   
   # 启动单币种服务
   ./scripts/deploy.sh start
   ```

## 测试建议

### 测试环境准备

1. **使用测试网络**
   - 在币安测试网络进行测试
   - 使用小额资金测试

2. **测试币种选择**
   - 建议测试 2-3 个币种
   - 选择流动性好的币种

3. **测试配置**
   ```yaml
   symbols:
     - name: BTCUSDT
       grid_spacing: 0.004
       initial_quantity: 0.001
       leverage: 20
     - name: ETHUSDT
       grid_spacing: 0.005
       initial_quantity: 0.01
       leverage: 20
   ```

### 测试验证步骤

1. **启动测试**
   ```bash
   ./scripts/deploy.sh multi-start
   ```

2. **检查日志**
   ```bash
   # 检查主日志
   tail -f log/multi_grid_BN.log
   
   # 检查状态汇总
   tail -f log/status_summary.log
   
   # 检查币种日志
   tail -f log/grid_BN_BTCUSDT.log
   tail -f log/grid_BN_ETHUSDT.log
   ```

3. **验证功能**
   - 确认两个币种都在运行
   - 确认日志文件正常生成
   - 确认 Telegram 通知正常

4. **健康检查**
   ```bash
   python3 health_check.py
   ```

## 技术支持

### 日志分析

如果遇到问题，请提供以下信息：

1. 主日志文件：`log/multi_grid_BN.log`
2. 状态汇总日志：`log/status_summary.log`
3. 相关币种日志：`log/grid_BN_[币种].log`
4. 健康检查结果：`python3 health_check.py`

### 常见错误

1. **"API_KEY 和 API_SECRET 必须设置"**
   - 检查 config/.env 文件是否存在
   - 确认 API_KEY 和 API_SECRET 已设置

2. **"配置文件不存在"**
   - 确认 symbols.yaml 或 symbols.json 文件存在
   - 检查文件格式是否正确

3. **"双向持仓模式失败"**
   - 在币安手动启用双向持仓模式
   - 确认 API 密钥有足够权限

4. **"WebSocket 连接失败"**
   - 检查网络连接
   - 确认防火墙设置
   - 检查 API 密钥权限

---

**注意**: 本软件仅供学习和研究使用，请在使用前充分了解风险，并确保遵守相关法律法规。
