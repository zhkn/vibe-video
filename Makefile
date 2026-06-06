.PHONY: help install install-dev run dev test lint format clean

PYTHON ?= python3
PORT ?= 8766
DATA_DIR ?= $(HOME)/Movies/GardenAutoCut

help: ## 显示帮助
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

install: ## 安装依赖
	$(PYTHON) -m pip install -r requirements.txt

install-dev: ## 安装开发依赖
	$(PYTHON) -m pip install -r requirements.txt
	$(PYTHON) -m pip install pytest ruff

run: ## 启动服务 (默认端口 8766)
	$(PYTHON) -m app.server --port $(PORT) --data-dir $(DATA_DIR)

dev: ## 开发模式启动 (自动重载)
	FLASK_DEBUG=1 $(PYTHON) -m app.server --port $(PORT) --data-dir $(DATA_DIR)

test: ## 运行测试
	$(PYTHON) -m pytest tests/ -v

lint: ## 代码检查
	ruff check app/ scripts/ tests/

format: ## 代码格式化
	ruff format app/ scripts/

clean: ## 清理缓存
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
