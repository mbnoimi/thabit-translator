# net9.0 SDK: matches Jellyfin 10.11's target framework, and unlike the 10.0
# SDK image it ships the 9.0 runtime the test host runs on.
FROM mcr.microsoft.com/dotnet/sdk:9.0-noble

# Ubuntu 24.04 marks the system Python externally managed; jprm is a pure-Python
# tool that just drives the dotnet SDK in this same container.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3-pip git \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --break-system-packages --no-cache-dir jprm==1.1.0

ENV PATH="/root/.dotnet/tools:${PATH}"

WORKDIR /src
