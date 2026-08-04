# Use a slim Python 3.11 base image
FROM python:3.11-slim

# Install system dependencies including Tesseract OCR
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Set up working directory
WORKDIR /app

# Copy and install python dependencies. `clients/` has to come along here, not
# with the rest of the source below: requirements.txt ends with an editable
# install of ./clients/python, and pip fails outright if that path is missing.
# It is copied separately so a source edit does not invalidate this layer.
COPY requirements.txt .
COPY clients ./clients
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Expose the API port
EXPOSE 8000

# Start command
CMD ["uvicorn", "app.api.routes:app", "--host", "0.0.0.0", "--port", "8000"]
