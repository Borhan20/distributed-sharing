FROM python:3.12-slim
# Dependencies go here (RUN pip install ...): jobs have no network while they run.
COPY train.py /app/train.py
ENTRYPOINT ["python", "/app/train.py"]
