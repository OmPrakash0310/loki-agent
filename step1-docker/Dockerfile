# 1. Start from a box that already has Python installed
FROM python:3.12-slim

# 2. Inside the box, work in a folder called /app
WORKDIR /app

# 3. Copy our code from your Mac into the box
COPY app.py .

# 4. When the box starts, run our server
CMD ["python", "app.py"]
