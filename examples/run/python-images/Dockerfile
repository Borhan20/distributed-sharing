# Image processing with Pillow. The package is installed here: jobs have no network while they run.
FROM python:3.12-slim
RUN pip install --no-cache-dir pillow==11.0.0
COPY make_images.py process.py /app/
# Made-up sample pictures, created while the image is built. Put your own pictures in /data instead.
RUN python /app/make_images.py /data
ENTRYPOINT ["python", "/app/process.py"]
