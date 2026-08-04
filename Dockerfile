FROM python:3.11-slim

ARG HERMES_AGENT_VERSION=0.15.2
ARG HERMES_AGENT_SOURCE=""
# 大陆 ECS 构建时传 --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HERMES_HOME=/home/aura/.hermes

RUN useradd --create-home --shell /bin/bash aura

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libopus0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

# Install the pinned PyPI Hermes CLI before copying Lily source so normal
# integration changes do not invalidate this heavier dependency layer.
RUN if [ -z "$HERMES_AGENT_SOURCE" ]; then \
        python -m pip install --no-cache-dir "hermes-agent==${HERMES_AGENT_VERSION}"; \
    fi

COPY integrations ./integrations

# Optional source-tree override. This runs after COPY because the source path
# must exist inside the build context.
RUN if [ -n "$HERMES_AGENT_SOURCE" ] && [ -d "$HERMES_AGENT_SOURCE" ]; then \
        python -m pip install --no-cache-dir "$HERMES_AGENT_SOURCE"; \
    fi

RUN mkdir -p /home/aura/.hermes /workspace /skills \
    && chown -R aura:aura /home/aura /workspace /skills /app

USER aura

EXPOSE 8765 8787

CMD ["python", "-m", "integrations.hermes_lily_cli.server", "--host", "0.0.0.0", "--port", "8765"]
