FROM python:3.11-slim

# Install python-telegram-bot + aiohttp
RUN pip install --no-cache-dir python-telegram-bot==20.8 aiohttp==3.9.5

# Copy your bot code
WORKDIR /app
COPY main.py .

# Run bot
CMD ["python", "main.py"]
