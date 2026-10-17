FROM python:3.12-slim
RUN pip install --no-cache-dir httpx==0.27.2
COPY loadgen.py .
CMD ["python", "-u", "loadgen.py"]
