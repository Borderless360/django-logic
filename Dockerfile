FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install dependencies first (cached unless pyproject.toml changes)
COPY pyproject.toml README.md ./
RUN mkdir -p django_logic && touch django_logic/__init__.py && \
    pip install --no-cache-dir -e ".[dev]" && \
    rm -rf django_logic

COPY . .
RUN pip install --no-cache-dir -e ".[dev]"

CMD ["python", "tests/manage.py", "runserver", "0.0.0.0:8000"]
