# Train a classifier with scikit-learn. Packages are installed here: jobs have no network while they run.
FROM python:3.12-slim
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY train.py /app/train.py
ENTRYPOINT ["python", "/app/train.py"]
