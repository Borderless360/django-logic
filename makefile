PROJECT_NAME = django-logic
DOCKER_RUN = docker run --rm -v $(CURDIR):/app $(PROJECT_NAME)

.PHONY: info build test test-one coverage sh stability-up stability-test stability-redis stability-down

info:
	@echo "Usage: make <target>"
	@echo "Targets:"
	@echo "  build            - Build the Docker image"
	@echo "  test             - Run unit tests (SQLite)"
	@echo "  test-one         - Run a single test (t=path.to.test)"
	@echo "  coverage         - Run tests with coverage report"
	@echo "  sh               - Run a Django shell"
	@echo "  stability-redis  - Run stability tests (SQLite + local Redis)"
	@echo "  stability-up     - Start Postgres + Redis via Docker Compose"
	@echo "  stability-test   - Run stability tests (Postgres + Redis)"
	@echo "  stability-down   - Stop Postgres + Redis"

build:
	docker build -t $(PROJECT_NAME) .

test:
	$(DOCKER_RUN) python tests/manage.py test

test-one:
ifndef t
	$(error Usage: make test-one t=path.to.TestCase)
endif
	$(DOCKER_RUN) python tests/manage.py test $(t)

coverage:
	$(DOCKER_RUN) sh -c "coverage run ./tests/manage.py test && coverage report && coverage html"

sh:
	docker run --rm -it -p 8000:8000 -v $(CURDIR):/app $(PROJECT_NAME) python tests/manage.py shell

stability-redis:
	DJANGO_SETTINGS_MODULE=tests.settings_redis \
	python tests/manage.py test tests.stability --verbosity=2 --tag=stability

stability-up:
	docker compose -f docker-compose.test.yml up -d postgres redis
	@echo "Waiting for services..."
	@sleep 3
	@echo "Postgres and Redis are ready."

stability-test:
	DJANGO_SETTINGS_MODULE=tests.settings_stability \
	python tests/manage.py test tests.stability --verbosity=2 --tag=stability

stability-down:
	docker compose -f docker-compose.test.yml down -v
