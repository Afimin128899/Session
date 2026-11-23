FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py launch.py login_diagnostics.py session_devices.py spam_check.py session_results.py ./
CMD ["python", "launch.py"]
