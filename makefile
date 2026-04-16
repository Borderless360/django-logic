PROJECT_NAME = django-logic
DOCKER_RUN = docker run --rm -v $(CURDIR):/app $(PROJECT_NAME)

.PHONY: info build test test-one coverage sh

info:
	@echo "Usage: make <target>"
	@echo "Targets:"
	@echo "  build    - Build the Docker image"
	@echo "  test     - Run the tests"
	@echo "  test-one - Run a single test (t=path.to.test)"
	@echo "  coverage - Run tests with coverage report"
	@echo "  sh       - Run a Django shell"

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
