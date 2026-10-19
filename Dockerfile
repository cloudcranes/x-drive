FROM python:3.12-slim

ARG RCLONE_VERSION=v1.75.2

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl unzip tzdata \
 && curl -fsSL "https://downloads.rclone.org/${RCLONE_VERSION}/rclone-${RCLONE_VERSION}-linux-amd64.zip" -o /tmp/rclone.zip \
 && unzip -q /tmp/rclone.zip -d /tmp \
 && mv /tmp/rclone-*/rclone /usr/local/bin/rclone \
 && rm -rf /tmp/rclone.zip /tmp/rclone-* \
 && apt-get purge -y unzip && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/* \
 && rclone version | head -1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "app.main"]
