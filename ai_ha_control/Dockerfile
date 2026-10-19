ARG BUILD_FROM
FROM ${BUILD_FROM}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apk add --no-cache python3 py3-pip py3-pillow tzdata

WORKDIR /opt/ai_ha_control
COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY app ./app
# Byte-code is compiled at build time: the AppArmor profile keeps the code directory read-only.
RUN python3 -m compileall -q app
COPY run.sh /run.sh
COPY backup.sh /usr/bin/ai-ha-backup
RUN chmod a+x /run.sh /usr/bin/ai-ha-backup

CMD [ "/run.sh" ]
