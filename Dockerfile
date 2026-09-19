# 项目二 · Execution-aware Alpha —— 容器镜像
#
# 为什么用 3.12-slim：代码要求 Python >= 3.11（用了 datetime.UTC），
# 且**零第三方依赖**，所以镜像里不需要 pip install 任何东西。
#
# ⚠️ 必须装 tzdata：common/market_calendar.py 用 zoneinfo 读
#    America/New_York 判"美股时段"，而 Debian slim 镜像默认没有 tz 数据库，
#    缺了会抛 ZoneInfoNotFoundError（本机 Windows 上不会暴露这个问题）。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LANG=C.UTF-8

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 只复制运行必需的东西（数据快照 40 MB 是必需的：本仓库离线自足）
COPY common/ ./common/
COPY project2/ ./project2/
COPY tools/ ./tools/
COPY web/ ./web/
COPY prompts/ ./prompts/
COPY docs/ ./docs/
COPY data/ ./data/
COPY run_p2.py README.md requirements.txt ./

# 容器里对外监听；平台会用 $PORT 覆盖端口（run_p2.py 认这个环境变量）
ENV PORT=8788
EXPOSE 8788

# 健康检查直接用本仓库自己的端点 —— 不引入额外工具
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8788')+'/api/health',timeout=8).read()" || exit 1

# 启动即自检会拖慢冷启动，所以这里只起服务；
# 想验证镜像完整性就手动跑：docker run --rm -it <image> python run_p2.py --selftest
CMD ["python", "run_p2.py", "--host", "0.0.0.0"]
