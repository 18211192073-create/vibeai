#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 自动加载本地环境变量，方便你直接把 key 写进 .env
if [ -f ".env" ]; then
    set -a
    # shellcheck disable=SC1091
    . ".env"
    set +a
fi

# 兼容你当前习惯的 OpenAI / Volc 写法，统一转成项目实际识别的 AI_* 变量
if [ -z "${AI_API_KEY:-}" ] && [ -n "${OPENAI_API_KEY:-}" ]; then
    export AI_API_KEY="${OPENAI_API_KEY}"
fi
if [ -z "${AI_BASE_URL:-}" ] && [ -n "${OPENAI_BASE_URL:-}" ]; then
    export AI_BASE_URL="${OPENAI_BASE_URL}"
fi
if [ -z "${AI_MODEL:-}" ] && [ -n "${OPENAI_MODEL:-}" ]; then
    export AI_MODEL="${OPENAI_MODEL}"
fi
if [ -z "${AI_API_KEY:-}" ] && [ -n "${VOLC_API_KEY:-}" ]; then
    export AI_API_KEY="${VOLC_API_KEY}"
fi
if [ -z "${AI_BASE_URL:-}" ] && [ -n "${VOLC_BASE_URL:-}" ]; then
    export AI_BASE_URL="${VOLC_BASE_URL}"
fi
if [ -z "${AI_MODEL:-}" ] && [ -n "${VOLC_MODEL:-}" ]; then
    export AI_MODEL="${VOLC_MODEL}"
fi

PORT="${MCP_PORT:-3333}"
HOST="${MCP_HOST:-0.0.0.0}"
PYTHON_BIN="${PYTHON_BIN:-}"

if [ -z "${PYTHON_BIN}" ]; then
    if [ -x ".venv/bin/python" ]; then
        PYTHON_BIN=".venv/bin/python"
    elif command -v python3 >/dev/null 2>&1; then
        PYTHON_BIN="$(command -v python3)"
    elif command -v python >/dev/null 2>&1; then
        PYTHON_BIN="$(command -v python)"
    else
        echo "❌ [错误] 找不到可用的 Python 解释器"
        exit 1
    fi
fi

echo "╔════════════════════════════════════════╗"
echo "║        DAY VIBE AI MCP Server         ║"
echo "╚════════════════════════════════════════╝"
echo ""

# 检查虚拟环境
if [ ! -d ".venv" ]; then
    echo "❌ [错误] 虚拟环境未找到"
    echo "请先运行 ./setup-mac.sh 进行部署"
    echo ""
    exit 1
fi

echo "[模式] HTTP (适合远程访问)"
echo "[地址] http://localhost:${PORT}/mcp"
echo "[提示] 按 Ctrl+C 停止服务"
echo ""

occupied_pids=""
if command -v lsof >/dev/null 2>&1; then
    occupied_pids="$(lsof -tiTCP:"${PORT}" -sTCP:LISTEN 2>/dev/null || true)"
fi

if [ -n "${occupied_pids}" ]; then
    same_service_pids=""
    for pid in ${occupied_pids}; do
        cmdline="$(ps -p "${pid}" -o command= 2>/dev/null || true)"
        if printf '%s' "${cmdline}" | grep -q "mcp_server.server"; then
            same_service_pids="${same_service_pids} ${pid}"
        else
            echo "❌ [错误] 端口 ${PORT} 已被其他进程占用：PID ${pid}"
            echo "   命令：${cmdline}"
            echo "   如果这是你想保留的服务，请改用 MCP_PORT=3334 ./start-http.sh"
            exit 1
        fi
    done

    if [ -n "${same_service_pids}" ]; then
        echo "[提示] 发现已有 MCP 服务占用端口 ${PORT}，正在自动结束旧进程..."
        kill ${same_service_pids} 2>/dev/null || true
        sleep 1
    fi
fi

"${PYTHON_BIN}" -m mcp_server.server --transport http --host "${HOST}" --port "${PORT}"
