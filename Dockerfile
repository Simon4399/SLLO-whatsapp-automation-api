FROM python:latest

WORKDIR /usr/src/app/whatsapp-api

# Prevent Python from writing .pyc files and buffer stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose port
EXPOSE 3004

# Run the application
CMD ["uvicorn", "src.server:app", "--host", "0.0.0.0", "--port", "3004"]
