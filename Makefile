.PHONY: up down reset lint format test check migrate samples

up:
	docker compose up -d --build --wait

down:
	docker compose down

reset:
	docker compose down -v

lint:
	ruff check .
	ruff format --check .

format:
	ruff check --fix .
	ruff format .

test:
	pytest

check:
	docker compose run --rm app check

migrate:
	docker compose run --rm app migrate

samples:
	python -m market_map generate-samples
