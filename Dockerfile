FROM ghcr.io/gormantec/docker-iot-base:latest

WORKDIR /usr/src/app

# ── Python deps (aiomqtt, aiohttp already in base) ──
COPY src/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Node deps ──
COPY ui/package.json ui/package.json
RUN cd ui && npm install

# ── Source files ──
COPY src/ .

# ── UI source and build ──
COPY ui/src/ ./ui/src/
COPY ui/vite.config.js ui/index.html ./ui/
RUN cd ui && npm run build && rm -rf node_modules

COPY start.sh .
RUN chmod +x start.sh

EXPOSE 9000 9094

CMD [ "./start.sh" ]
