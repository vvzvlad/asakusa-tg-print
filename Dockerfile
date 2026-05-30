FROM python:3.11-slim

WORKDIR /app

# cups-client provides the `lp` binary used to submit jobs over IPP
RUN apt-get update \
    && apt-get install -y --no-install-recommends cups-client \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code and the label template
COPY src/ src/
COPY data/label_template.json data/label_template.json
COPY main.py .

# Run the bot
CMD ["python", "main.py"]
