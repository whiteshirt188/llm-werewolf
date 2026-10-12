.PHONY: demo agent-demo api api-demo test lint web-build web-check web-dev play

SEED ?= 7
PLAYERS ?= 6
NODE_BIN := $(CURDIR)/.runtime/node-v24.21.0-darwin-arm64/bin
NPM := $(if $(wildcard $(NODE_BIN)/npm),$(NODE_BIN)/npm,npm)

demo:
	cd backend && ../.venv/bin/python -m werewolf.demo --seed $(SEED) --players $(PLAYERS)

agent-demo:
	cd backend && ../.venv/bin/python -m werewolf.agent_demo --seed $(SEED)

api:
	.venv/bin/python -m uvicorn werewolf.api:app --app-dir backend --host 127.0.0.1 --port 8000 --timeout-graceful-shutdown 3

api-demo:
	cd backend && ../.venv/bin/python -m werewolf.api_demo --seed $(SEED)

test:
	cd backend && ../.venv/bin/python -m pytest -q

lint:
	.venv/bin/ruff check backend/werewolf backend/tests

web-build:
	PATH="$(NODE_BIN):$$PATH" "$(NPM)" --prefix frontend run build

web-check:
	PATH="$(NODE_BIN):$$PATH" "$(NPM)" --prefix frontend run typecheck
	PATH="$(NODE_BIN):$$PATH" "$(NPM)" --prefix frontend test

web-dev:
	PATH="$(NODE_BIN):$$PATH" "$(NPM)" --prefix frontend run dev

play: web-build
	$(MAKE) api
