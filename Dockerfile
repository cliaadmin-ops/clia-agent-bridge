# Stage 1: Build Tailwind
FROM node:20-slim AS builder
WORKDIR /app
COPY . .
RUN npm install -D tailwindcss
RUN npx tailwindcss -i ./static/css/input.css -o ./static/css/main.css --minify

# Stage 2: Final Python Image
FROM python:3.11-slim

# Install git
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Copy the compiled CSS from the builder stage
COPY --from=builder /app/static/css/main.css ./static/css/main.css

# Set environment variables
ENV REPO_PATH=/tmp/clia-website
ENV ENV=prod

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
