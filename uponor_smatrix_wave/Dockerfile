FROM python:3.12-slim-bookworm
ARG BUILD_VERSION=0.2.0
ARG BUILD_ARCH=amd64
LABEL io.hass.version="${BUILD_VERSION}" io.hass.type="app" io.hass.arch="${BUILD_ARCH}" \
      org.opencontainers.image.title="Home Assistant App: Uponor Smatrix Wave X-165" \
      org.opencontainers.image.description="Unofficial receive-only X-165 and T-165 MQTT bridge" \
      org.opencontainers.image.source="https://github.com/idoiten/uponor-x165" \
      org.opencontainers.image.licenses="MIT"
ENV PYTHONUNBUFFERED=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
RUN apt-get update && apt-get install -y --no-install-recommends rtl-sdr ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY uponor_smatrix_wave_x165 ./uponor_smatrix_wave_x165
COPY uponor ./uponor
RUN pip install --no-cache-dir ".[receiver,mqtt]"
ENTRYPOINT ["python", "-m", "uponor_smatrix_wave_x165.app"]
