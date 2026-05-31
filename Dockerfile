FROM python:3.12-slim

WORKDIR /app

# cups-client provides the `lp` binary used to submit jobs over IPP
RUN apt-get update \
    && apt-get install -y --no-install-recommends cups-client \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies (separate layer — changes less often than the code)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Runtime state directory (mounted as a docker volume in production)
RUN mkdir -p data

# Copy source code and the label templates (static code assets)
COPY src/ src/
COPY templates/ templates/
COPY main.py .

# Run the bot
CMD ["python", "main.py"]
