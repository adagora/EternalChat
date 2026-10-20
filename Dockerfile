# EternalChat's web page in a container, with the Codex and Claude Code CLIs for
# subscription mode. Everything that must last lives on the volume at /data: the
# chat, its settings and web token, and the CLIs' logins. deploy/fly.sh runs it
# on Fly.io; any host that keeps /data works the same way.
FROM node:24-bookworm-slim

# EternalChat needs only Python's standard library (Debian 12 has 3.11).
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# The subscription CLIs, at their newest versions on each deploy.
RUN npm install -g @openai/codex @anthropic-ai/claude-code && npm cache clean --force

WORKDIR /app
COPY eternalchat/ eternalchat/
COPY deploy/entrypoint.sh deploy/machine.sh deploy/

ENV ETERNALCHAT_HOME=/data/eternalchat \
    HOME=/data/home \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    DISABLE_AUTOUPDATER=1

EXPOSE 8765
CMD ["sh", "/app/deploy/entrypoint.sh"]
