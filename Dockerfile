FROM python:3.11-alpine

WORKDIR /usr/src/app

# Install system deps: git for cloning repo, nodejs+npm for building React UI
RUN apk add --no-cache git bash nodejs npm

RUN pip install --no-cache-dir --upgrade pip

# ── Stage 1: Copy requirements and install Python deps ──
COPY src/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Stage 2: Copy UI package.json and install Node deps ──
COPY ui/package.json ui/package.json
RUN cd ui && npm install

# ── Stage 3: Copy source files ──
COPY src/ .

# Copy build script and run it to extract ocpp library files
COPY build.sh .
RUN bash build.sh
RUN rm -f build.sh

# ── Stage 4: Copy UI source and build React app ──
COPY ui/src/ ./ui/src/
COPY ui/vite.config.js ui/index.html ./ui/
RUN cd ui && npm run build && rm -rf node_modules

# Copy startup script
COPY start.sh .
RUN chmod +x start.sh

EXPOSE 9000 9094

CMD [ "./start.sh" ]
