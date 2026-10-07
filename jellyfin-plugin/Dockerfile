# Runtime image: Jellyfin 10.11 + python3 + the plugin.
#
# Build order (see ../README.md):
#   1. docker compose run --rm plugin-build     -> artifacts/*.zip
#   2. docker compose build jellyfin
#
# No library and no venv in this image: the plugin DLL embeds src/core/*.py
# and extracts it into /config/plugins/.../library on first start; the CLI
# bootstraps its own venv there (python3 -m venv .../.venv_thabit) on first run
# or via the "Prepare runtime" button. The build context is src/, and
# jellyfin-plugin/Dockerfile.dockerignore allow-lists every file copied below -
# src/core never enters the context at all, so its thabit_translator.conf
# credentials cannot reach any layer.
FROM jellyfin/jellyfin:10.11

# 1. Python is not in the base image; ffmpeg/ffprobe exist but only under
#    /usr/lib/jellyfin-ffmpeg, which is not on PATH for child processes.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv python3-pip ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/lib/jellyfin-ffmpeg/ffmpeg /usr/local/bin/ffmpeg \
    && ln -sf /usr/lib/jellyfin-ffmpeg/ffprobe /usr/local/bin/ffprobe

# 2. The plugin zip produced by `jprm plugin build`, unpacked into a staging
#    folder; the entrypoint copies it into /config/plugins on every start so a
#    rebuilt image updates the plugin instead of leaving the old DLL behind.
COPY jellyfin-plugin/artifacts/ /tmp/plugin-artifacts/
RUN python3 -c "import glob, zipfile; archives = glob.glob('/tmp/plugin-artifacts/*.zip'); \
assert archives, 'no plugin zip in jellyfin-plugin/artifacts - run plugin-build first'; \
zipfile.ZipFile(archives[0]).extractall('/opt/thabit/plugin')" \
    && rm -rf /tmp/plugin-artifacts

COPY jellyfin-plugin/docker/entrypoint.sh /opt/thabit/entrypoint.sh
RUN chmod +x /opt/thabit/entrypoint.sh \
    && chmod -R a+rX /opt/thabit

ENTRYPOINT ["/opt/thabit/entrypoint.sh"]
