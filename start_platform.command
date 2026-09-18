#!/bin/bash
# 双击即可启动测试平台可视化界面（macOS）
# 服务以独立进程组运行，关闭终端不影响；浏览器打开 http://127.0.0.1:8765
#
# 运行环境：使用脚本所在目录下的 .venv（与 run_pipeline.py / server.py 一致），
# 避免依赖宿主系统 Python；克隆仓库后只需 `python3 -m venv .venv && pip install -r requirements.txt` 即可双击启动。
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$DIR/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "找不到 $PY" >&2
  echo "请先创建项目虚拟环境：python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

if curl -s --noproxy '*' -m 2 -o /dev/null http://127.0.0.1:8765/api/rounds; then
  echo "服务已在运行"
else
  echo "启动测试平台服务..."
  "$PY" - "$DIR" <<'PYEOF'
import subprocess, sys, os
os.chdir(sys.argv[1])
log_dir = os.path.join(sys.argv[1], "logs")
os.makedirs(log_dir, exist_ok=True)
subprocess.Popen([sys.executable, "server.py", "--port", "8765"],
                 stdout=open(os.path.join(log_dir, "platform.log"), "w"), stderr=subprocess.STDOUT,
                 stdin=subprocess.DEVNULL, start_new_session=True)
PYEOF
  sleep 2
fi

open "http://127.0.0.1:8765"
echo "已打开浏览器：http://127.0.0.1:8765"
