FROM python:3.10-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip to fix wheel metadata bugs and install typing_extensions
RUN pip install --no-cache-dir --upgrade pip setuptools wheel typing_extensions

# Copy requirements and install CPU-only PyTorch using extra-index-url
COPY requirements.txt .
RUN pip install --no-cache-dir torch torchvision --extra-index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Default Environment Variables
ENV PORT=8000
ENV LEAFWATCH_ADMIN_KEY=myadmin123

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
