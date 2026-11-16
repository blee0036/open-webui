FROM ghcr.io/open-webui/open-terminal:0.17.7
USER root
# The upstream full image has passwordless sudo and a capability-bearing Python.
# Managed workspaces deliberately do not use either facility or its entrypoint.
RUN setcap -r /usr/local/bin/python3-ot \
    && sed -i '/user ALL=(ALL) NOPASSWD:ALL/d' /etc/sudoers \
    && find / -xdev -type f \( -perm -4000 -o -perm -2000 \) -exec chmod a-s {} + \
    && mkdir -p /opt/team && chown -R 1000:1000 /home/user
COPY sandbox_tool.py /opt/team/sandbox_tool.py
RUN mv /usr/bin/chromium /usr/bin/chromium-upstream
COPY --chmod=755 chromium.py /usr/bin/chromium
ENV OPEN_TERMINAL_MULTI_USER=false OPEN_TERMINAL_FILE_BROWSER_ROOT=/home/user \
    OPEN_TERMINAL_EXECUTE_TIMEOUT=120 OPEN_TERMINAL_ENABLE_NOTEBOOKS=false \
    HOME=/home/user USER=user HTTP_PROXY=http://egress.invalid:3128 HTTPS_PROXY=http://egress.invalid:3128 \
    NO_PROXY=localhost,127.0.0.1 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1
USER 1000:1000
WORKDIR /home/user
ENTRYPOINT ["/usr/bin/tini", "--", "/bin/sh", "-c", "while [ ! -f /run/network-ready ]; do sleep 0.1; done; exec open-terminal run --host 0.0.0.0 --port 8000"]
