#!/bin/bash

# 网格交易机器人启动脚本
# 支持单币种和多币种模式

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

print_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

check_requirements() {
    print_info "检查运行环境..."
    
    # 检查 Python
    if ! command -v python3 &> /dev/null; then
        print_error "Python3 未安装"
        exit 1
    fi
    
    # 检查必要文件
    if [ ! -f "config/.env" ]; then
        print_error "config/.env 文件不存在，请先配置环境变量"
        exit 1
    fi
    
    # 检查依赖
    if ! python3 -c "import ccxt, websockets, yaml" 2>/dev/null; then
        print_warning "缺少依赖包，正在安装..."
        pip3 install ccxt websockets python-dotenv pyyaml aiohttp
    fi
    
    print_success "环境检查完成"
}

start_single_bot() {
    print_info "启动单币种网格机器人..."

	# 加载环境变量
	if [ -f "config/.env" ]; then
		set -a
		source config/.env
		set +a
	else
		print_error "config/.env 文件不存在，请先配置环境变量"
		exit 1
	fi

	# 根据 EXCHANGE 分支
	exchange="${EXCHANGE:-gate}"
	exchange_lc="${exchange,,}"
	print_info "选择的交易所: ${exchange_lc}"

	# 启动前的文件检查
	if [ "${exchange_lc}" = "binance" ]; then
		if [ ! -f "src/single_bot/binance_bot.py" ]; then
			print_error "src/single_bot/binance_bot.py 文件不存在"
			exit 1
		fi
	elif [ "${exchange_lc}" = "gate" ]; then
		if [ ! -f "src/single_bot/gate_bot.py" ]; then
			print_error "src/single_bot/gate_bot.py 文件不存在"
			exit 1
		fi
	else 
		print_error "不支持的 EXCHANGE 值: ${EXCHANGE}，应为 binance 或 gate"
		exit 1
	fi

	# 创建 PID 文件
	echo $$ > grid_bot.pid

	# 按交易所启动单币种机器人
	if [ "${exchange_lc}" = "binance" ]; then
		python3 src/single_bot/binance_bot.py
	else
		python3 src/single_bot/gate_bot.py
	fi

	# 清理 PID 文件
	rm -f grid_bot.pid
}

start_multi_bot() {
    print_info "启动多币种网格机器人..."
    
    if [ ! -f "src/multi_bot/multi_bot.py" ]; then
        print_error "src/multi_bot/multi_bot.py 文件不存在"
        exit 1
    fi
    
    # 加载环境变量
    if [ -f "config/.env" ]; then
        set -a
        source config/.env
        set +a
    else
        print_error "config/.env 文件不存在，请先配置环境变量"
        exit 1
    fi

    # 检查配置文件
    if [ ! -f "config/symbols.yaml" ] && [ ! -f "config/symbols.json" ]; then
        print_error "配置文件不存在，请创建 config/symbols.yaml 或 config/symbols.json"
        exit 1
    fi
    
    # 创建 PID 文件
    echo $$ > grid_bot.pid
    
    # 启动多币种机器人
    python3 src/multi_bot/multi_bot.py
    
    # 清理 PID 文件
    rm -f grid_bot.pid
}

show_help() {
    echo "网格交易机器人启动脚本"
    echo ""
    echo "使用方法: $0 [选项]"
    echo ""
    echo "选项:"
    echo "  single     - 启动单币种模式 (默认)"
    echo "  multi      - 启动多币种模式"
    echo "  docker     - 使用 Docker 启动"
    echo "  docker-multi - 使用 Docker 启动多币种模式"
    echo "  logs       - 查看日志"
    echo "  status     - 查看状态"
    echo "  stop       - 停止机器人"
    echo "  help       - 显示此帮助信息"
    echo ""
    echo "示例:"
    echo "  $0 single      # 启动单币种模式"
    echo "  $0 multi       # 启动多币种模式"
    echo "  $0 docker      # 使用 Docker 启动"
    echo "  $0 logs        # 查看日志"
}

show_logs() {
    print_info "查看日志..."
    
    if [ -f "log/multi_grid_BN.log" ]; then
        echo "=== 多币种主日志 ==="
        tail -f log/multi_grid_BN.log
    elif [ -f "log/grid_BN.log" ]; then
        echo "=== 单币种日志 ==="
        tail -f log/grid_BN.log
    else
        print_warning "未找到日志文件"
    fi
}

show_status() {
    print_info "查看状态..."
    
    if [ -f "grid_bot.pid" ]; then
        PID=$(cat grid_bot.pid)
        if ps -p $PID > /dev/null; then
            print_success "机器人正在运行，PID: $PID"
        else
            print_warning "PID 文件存在但进程已停止"
            rm -f grid_bot.pid
        fi
    else
        print_warning "机器人未运行"
    fi
    
    # 显示状态汇总
    if [ -f "log/status_summary.log" ]; then
        echo ""
        echo "=== 最新状态汇总 ==="
        tail -5 log/status_summary.log
    fi
}

stop_bot() {
    print_info "停止机器人..."
    
    if [ -f "grid_bot.pid" ]; then
        PID=$(cat grid_bot.pid)
        if ps -p $PID > /dev/null; then
            kill $PID
            print_success "已发送停止信号"
        else
            print_warning "进程已停止"
        fi
        rm -f grid_bot.pid
    else
        print_warning "未找到 PID 文件"
    fi
}

# 主逻辑
# 检查环境变量决定运行模式
if [ "$GRID_MODE" = "multi" ]; then
    DEFAULT_MODE="multi"
else
    DEFAULT_MODE="single"
fi

case "${1:-$DEFAULT_MODE}" in
    "single")
        check_requirements
        start_single_bot
        ;;
    "multi")
        check_requirements
        start_multi_bot
        ;;
    "docker")
        ./deploy.sh start
        ;;
    "docker-multi")
        ./deploy.sh multi-start
        ;;
    "logs")
        show_logs
        ;;
    "status")
        show_status
        ;;
    "stop")
        stop_bot
        ;;
    "help" | "--help" | "-h")
        show_help
        ;;
    *)
        print_error "未知选项: $1"
        show_help
        exit 1
        ;;
esac 
