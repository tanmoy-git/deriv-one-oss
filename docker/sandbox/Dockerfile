# The image model-generated Python runs in.
#
# The default (python:3.12-slim) has nothing beyond the standard library, and a
# run has no network, so code cannot install what it needs at runtime. This
# image pre-installs the libraries the run_python tool description advertises.
# Keep the two in step: if you add a package here, add it to
# SANDBOX_PYTHON_PACKAGES so the model is told about it.
#
# docker compose builds and tags this automatically. To build it by hand:
#   docker build -t platform/sandbox-python:latest docker/sandbox/
FROM python:3.12-slim@sha256:6b25cbe6577518889d553cdd5a5da97d259348d8f6e45b527dd731caaba27a07

RUN pip install --no-cache-dir \
    requests==2.32.3 \
    httpx==0.27.2 \
    curl-cffi==0.7.4 \
    beautifulsoup4==4.12.3 \
    lxml==5.3.0 \
    openpyxl==3.1.5 \
    xlrd==2.0.1 \
    tabulate==0.9.0 \
    python-dotenv==1.0.1 \
    "python-docx>=1.1.0" \
    slack_sdk==3.34.0 \
    pandas==2.2.3 \
    numpy==2.2.6 \
    matplotlib==3.10.3

# Runs execute with a read-only root filesystem, as a uid that owns nothing but
# the workspace, with only /tmp writable. Point everything that wants a home or
# a cache at /tmp, and stop CPython writing bytecode next to the code — the
# workspace is collected as run output afterwards, and __pycache__ is not output.
ENV HOME=/tmp \
    MPLCONFIGDIR=/tmp/matplotlib \
    XDG_CACHE_HOME=/tmp/cache \
    PYTHONDONTWRITEBYTECODE=1
