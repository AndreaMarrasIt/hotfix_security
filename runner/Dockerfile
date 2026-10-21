FROM node:22-bookworm-slim
ENV COPILOT_AUTO_UPDATE=false HOME=/home/node GH_PROMPT_DISABLED=1
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates curl bash coreutils \
    && npm install -g @github/copilot \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /workspace /opt/runner && chown -R node:node /workspace /opt/runner
COPY --chown=node:node run.sh /opt/runner/run.sh
RUN chmod 0555 /opt/runner/run.sh
WORKDIR /workspace
USER node
ENTRYPOINT ["/bin/bash", "/opt/runner/run.sh"]
