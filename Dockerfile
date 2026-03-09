FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir .[prod]
COPY . .

EXPOSE 8080
CMD ["python", "-m", "bot"]
