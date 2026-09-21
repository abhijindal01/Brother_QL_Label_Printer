# Label Bench Makefile
.PHONY: up build down restart logs status health watchdog loop

up:
	docker compose up -d

build:
	docker compose up -d --build

down:
	docker compose down

restart:
	docker compose restart

logs:
	docker compose logs -f label-bench

status:
	curl -s http://127.0.0.1:8013/api/status

health:
	curl -s http://127.0.0.1:8013/api/health

watchdog:
	./watchdog.sh

# Automatic loop: runs docker compose up -d every 30 seconds
loop:
	@echo "Running 'docker compose up -d' every 30 seconds. Press Ctrl+C to stop."
	@while true; do docker compose up -d; sleep 30; done
