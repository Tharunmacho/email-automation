# Use a slim Python 3.11 base image
FROM python:3.11-slim

# Install system dependencies including Tesseract OCR
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# One OpenMP thread per Tesseract process.
#
# Tesseract is built with OpenMP, and OpenMP sizes its pool from
# `sysconf(_SC_NPROCESSORS_ONLN)` — the *host's* core count, which knows nothing
# about the cgroup quota this container runs under. On an eight-core host with a
# four-CPU quota, the four Tesseract processes the reader admits concurrently
# each start eight threads: thirty-two threads fighting over four cores, and a
# 300-DPI page that should read in four seconds instead spends forty-five and
# times out. The pages did not get harder; they got starved.
#
# The parallelism this pipeline wants is across *pages*, and it already has it —
# `local_ocr` admits one page per available CPU. Threading each of those reads
# as well only oversubscribes the same cores. `app/extraction/local_ocr.py` sets
# this in-process too, so a host that runs the app outside this image gets the
# same behaviour; it is repeated here so the container is right on its own.
ENV OMP_THREAD_LIMIT=1

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
