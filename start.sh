#!/bin/bash
# ========================================
#   配表代码版本Diff平台 - 启动脚本 (Linux/macOS)
# ========================================

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}  配表代码版本Diff平台 - 启动脚本${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

# 检查 Python 是否安装（优先 python3）
#
# **判据是「能不能真的跑起来」，不是「PATH 里有没有这个名字」。**
# Windows 上 `python3` 会被「应用执行别名」接管：`command -v python3` 命中一个转发桩，
# 而它执行时只往 stderr 写一句「Python was not found; … Microsoft Store …」并以 **49**
# 退出（连 `--version` 都这样）。老写法（`command -v` 判存在）于是把一个**跑不了的**
# 解释器写进 PYTHON_CMD：版本号读成空串，后面的建虚拟环境、装依赖、读 .env 全部失败 ——
# 而报错位置离真正的原因隔了十几行（实测 `bash start.sh` 以 49 退出，看不出为什么）。
# 所以这里真去跑一次 `-c`，跑得通才算数。
_pick_python() {
    for _candidate in python3 python; do
        command -v "$_candidate" &> /dev/null || continue
        if "$_candidate" -c "import sys" &> /dev/null; then
            echo "$_candidate"
            return 0
        fi
    done
    return 1
}

if ! PYTHON_CMD="$(_pick_python)"; then
    echo -e "${RED}[错误] 未检测到可用的 Python，请先安装 Python 3.8+${NC}"
    echo -e "${YELLOW}      若已经装过却仍报这个错：Windows 上多半是「应用执行别名」接管了${NC}"
    echo -e "${YELLOW}      python3 —— 设置 → 应用 → 高级应用设置 → 应用执行别名，关掉它的开关。${NC}"
    exit 1
fi

echo -e "${GREEN}[信息] 检测到 Python:${NC}"
$PYTHON_CMD --version
echo ""

# 检查 Python 版本是否 >= 3.8
PY_VERSION=$($PYTHON_CMD -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$($PYTHON_CMD -c "import sys; print(sys.version_info.major)")
PY_MINOR=$($PYTHON_CMD -c "import sys; print(sys.version_info.minor)")

if [ "$PY_MAJOR" -lt 3 ] || ([ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 8 ]); then
    echo -e "${RED}[错误] Python 版本需要 >= 3.8，当前版本: ${PY_VERSION}${NC}"
    exit 1
fi

# 检查虚拟环境是否存在
USE_VENV=0
if [ ! -d "venv" ]; then
    echo -e "${YELLOW}[信息] 未检测到虚拟环境，正在创建...${NC}"
    $PYTHON_CMD -m venv venv
    if [ $? -eq 0 ]; then
        echo -e "${GREEN}[信息] 虚拟环境创建成功${NC}"
        USE_VENV=1
    else
        echo -e "${YELLOW}[警告] 虚拟环境创建失败，将使用全局 Python 环境${NC}"
    fi
else
    USE_VENV=1
fi

# 激活虚拟环境
if [ "$USE_VENV" -eq 1 ]; then
    echo -e "${GREEN}[信息] 激活虚拟环境...${NC}"
    source venv/bin/activate
    if [ $? -ne 0 ]; then
        echo -e "${YELLOW}[警告] 虚拟环境激活失败，将使用全局 Python 环境${NC}"
    fi
fi

# PIP 命令
#
# 直接用 `$PYTHON_CMD -m pip`，不在 PATH 上另找一个 pip —— 与上面同一类理由：PATH 上的
# `pip3` 可能是**另一个解释器**的（装进去的依赖跟真正启动应用的那个无关），也可能又是一个
# 跑不了的转发桩。找不到 pip 时按原样只警告一句、继续启动。
PIP_CMD=""
if $PYTHON_CMD -m pip --version &> /dev/null; then
    PIP_CMD="$PYTHON_CMD -m pip"
else
    echo -e "${YELLOW}[警告] 未找到 pip，跳过依赖安装${NC}"
fi

# 安装依赖
if [ -n "$PIP_CMD" ] && [ -f "requirements.txt" ]; then
    echo -e "${GREEN}[信息] 正在检查并安装依赖...${NC}"
    $PIP_CMD install -r requirements.txt -q
    if [ $? -eq 0 ]; then
        echo -e "${GREEN}[信息] 依赖安装完成${NC}"
    else
        echo -e "${YELLOW}[警告] 部分依赖安装可能失败，尝试继续启动...${NC}"
    fi
elif [ ! -f "requirements.txt" ]; then
    echo -e "${YELLOW}[警告] 未找到 requirements.txt 文件${NC}"
fi

echo ""
echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}  正在启动应用...${NC}"
echo -e "${BLUE}  按 Ctrl+C 停止服务${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

# 检查 .env 配置并修复格式；缺失时自动生成
echo -e "${GREEN}[信息] 检查 .env 配置并修复格式...${NC}"
if ! $PYTHON_CMD -m utils.env_bootstrap --env-path ".env"; then
    echo -e "${YELLOW}[警告] .env bootstrap 执行失败${NC}"
    if [ ! -f ".env" ]; then
        if [ -f ".env.simple" ]; then
            cp .env.simple .env
            echo -e "${YELLOW}[提示] 已从 .env.simple 复制默认配置，请手动修改密钥${NC}"
        else
            echo -e "${RED}[错误] 未找到 .env.simple，无法创建 .env${NC}"
            exit 1
        fi
    else
        echo -e "${RED}[错误] .env 已存在但 bootstrap 失败，请检查 Python traceback${NC}"
        exit 1
    fi
fi
echo ""

# 设置环境变量
export FLASK_APP=app.py
export FLASK_ENV=production
export PYTHONIOENCODING=utf-8

# 捕获退出信号
cleanup() {
    echo ""
    echo -e "${YELLOW}[信息] 正在停止应用...${NC}"
    kill $APP_PID 2>/dev/null
    wait $APP_PID 2>/dev/null
    echo -e "${GREEN}[信息] 应用已停止${NC}"
    exit 0
}
trap cleanup SIGINT SIGTERM

# 启动应用
$PYTHON_CMD app.py &
APP_PID=$!

echo -e "${GREEN}[信息] 应用 PID: ${APP_PID}${NC}"

# 等待应用退出
wait $APP_PID
EXIT_CODE=$?

echo ""
if [ $EXIT_CODE -eq 0 ]; then
    echo -e "${GREEN}[信息] 应用已正常停止${NC}"
else
    echo -e "${RED}[错误] 应用异常退出，退出码: ${EXIT_CODE}${NC}"
fi
