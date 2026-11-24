FROM python:3.13-alpine
RUN adduser -D -u 1000 app && mkdir /data && chown app /data
COPY ghsync.py /app/ghsync.py
USER app
ENV DATA_DIR=/data PYTHONUNBUFFERED=1
VOLUME /data
ENTRYPOINT ["python", "/app/ghsync.py"]
CMD ["run"]
