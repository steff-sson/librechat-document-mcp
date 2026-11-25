FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-venv \
        pandoc \
        libreoffice-writer-nogui \
        libreoffice-calc-nogui \
        poppler-utils \
        fonts-dejavu-core \
        fonts-liberation \
        ca-certificates \
        libxml2 \
        libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/toolbox/.venv
ENV PATH="/opt/toolbox/.venv/bin:$PATH"

RUN pip install --no-cache-dir \
        "mcp>=1.2,<2" \
        "pyjwt>=2.8,<3" \
        "requests>=2.31,<3" \
        "python-docx>=1.1,<2" \
        "docx-revisions>=0.1.5,<1" \
        "openpyxl>=3.1,<4" \
        "pymupdf>=1.24,<2" \
        "lxml>=5,<7"

WORKDIR /opt/toolbox
COPY server.py /opt/toolbox/server.py
COPY tools/ /opt/toolbox/tools/

RUN useradd --system --uid 10001 --create-home toolbox \
    && chown -R toolbox:toolbox /opt/toolbox
USER toolbox

EXPOSE 8080
HEALTHCHECK --interval=20s --timeout=5s --start-period=25s --retries=3 \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=3).status==200 else 1)"

CMD ["python3", "/opt/toolbox/server.py"]
