# 银行 AI 智能体 · 常用命令
# 默认用 .venv 里的解释器（没有 .venv 就退回系统 python3）；make venv 可新建隔离环境。

#: 解释器：优先用仓库自带的 .venv，其次退回系统 python3（本机没有 python3.12 这个名字）
PY ?= $(shell if [ -x .venv/bin/python ]; then echo .venv/bin/python; else echo python3; fi)
#: 建 venv 时用的索引；本机 pip.conf 里有个解析不了的 extra-index-url，必须绕开
PIP_INDEX ?= https://pypi.tuna.tsinghua.edu.cn/simple
VENV_PY ?= .venv/bin/python

.PHONY: help setup install venv run seed reseed demo-db demo-run test cov docs-check redteam smoke browser-smoke journeys latency fresh clean reset check-env

# make setup 的透传参数，例如 make setup ARGS="--dry-run" 或 ARGS="--ollama"
ARGS ?=

help:
	@echo 可用命令：
	@echo   make setup     一键配置（新电脑：建 .venv、装依赖、生成 .env、选模型、建库、自检）
	@echo   make setup ARGS="--dry-run"   只打印计划不动文件；ARGS="--ollama"/"--mock" 指定模型来源
	@echo   make check-env 打印解释器版本
	@echo   make venv     建 .venv 并装依赖（绕开本机 pip.conf 的坏索引）
	@echo   make run       启动服务 http://127.0.0.1:8000
	@echo   make demo-db   造一份满数据演示库 data/demo_full.db（不碰 data/bank.db）
	@echo   make demo-run  用演示库启动服务（六个页签都不是空态）
	@echo   make seed      建库与种子数据（已有数据则跳过）
	@echo   make reseed    删库重建（演示跑过转账、余额变了之后用）
	@echo   make test      跑 pytest
	@echo   make docs-check 校验文档里的数字与 release_manifest.json 一致
	@echo   make redteam   跑 43 条红队用例
	@echo   make smoke     模型连通性自检
	@echo   make browser-smoke 真浏览器冒烟（Playwright + Chromium，截图进 docs/交付/截图/）
	@echo   make latency  量一次「一句话到确认卡」的脚本桩端到端耗时
	@echo   make fresh     从零到可演示
	@echo   make clean     清理缓存与数据库

setup:
	bash scripts/setup.sh $(ARGS)

check-env:
	$(PY) --version

install:
	@echo 已使用系统 $(PY)；如需自建环境： make venv

venv:
	$(PY) -m venv .venv
	@# PIP_CONFIG_FILE=/dev/null + --isolated 双保险：本机 ~/.config/pip/pip.conf 里
	@# 的 extra-index-url 指向一个解析不了的域名（pypi.ngc.nvidia.com），
	@# 不清掉它每个包都会报 Failed to establish a new connection: [Errno -2]。
	PIP_CONFIG_FILE=/dev/null $(VENV_PY) -m pip install --disable-pip-version-check --isolated \
		--index-url $(PIP_INDEX) -r requirements.txt
	@echo ✓ 已装好；用 $(VENV_PY) -m pytest -q tests/ 验证

run:
	@test -f data/api_token && printf '浏览器地址（带访问令牌）：http://127.0.0.1:8000/#token=%s\n' "$$(cat data/api_token)" || true
	$(PY) -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

seed:
	$(PY) -m scripts.seed

reseed:
	$(PY) -m scripts.seed --force

# 演示库：另一份文件，六个页签都不是空态；不碰 data/bank.db
demo-db:
	$(VENV_PY) scripts/make_demo_db.py --force

demo-run:
	@BANK_DB_PATH=data/demo_full.db $(VENV_PY) -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

latency:
	PYTHONPATH=. $(VENV_PY) scripts/measure_latency.py

test:
	$(PY) -m pytest -q tests/

docs-check:
	$(VENV_PY) scripts/check_docs_numbers.py

cov:
	@echo 未安装 pytest-cov 时跳过；安装后本命令输出覆盖率
	$(PY) -m pytest -q tests/ --cov=app --cov-report=term-missing || $(PY) -m pytest -q tests/

redteam:
	$(PY) -m scripts.redteam

smoke:
	$(PY) -m app.smoke

browser-smoke:
	$(PY) scripts/browser_smoke.py

# 七条高风险浏览器旅程：真浏览器 + 真后端，同时断言页面文案与服务端状态。
# playwright 装在系统 python3.12 里（.venv 没装），所以这里用 JOURNEY_PY 而不是 $(PY)。
JOURNEY_PY ?= $(shell if [ -x /home/peng/.local/bin/python3.12 ]; then echo /home/peng/.local/bin/python3.12; else echo python3; fi)
journeys:
	$(JOURNEY_PY) scripts/ui_journeys.py $(ARGS)

fresh: clean seed test
	@echo ✓ 从零到可演示完成（fresh 已含 clean，重建的是干净库），执行 make run 启动

reset:
	@curl -s -X POST -H "Authorization: Bearer $$(cat data/api_token 2>/dev/null)" http://127.0.0.1:8000/api/demo/reset || echo 服务未启动

clean:
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -f data/bank.db data/bank.db-wal data/bank.db-shm
	@echo ✓ 已清理
