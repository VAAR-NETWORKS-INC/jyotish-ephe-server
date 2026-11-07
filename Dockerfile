# 3.11, not 3.12: pyswisseph 2.10.3.2 publishes Linux wheels only through CPython 3.11.
# On 3.12 pip compiles from source and fails in this image (no libc headers).
FROM python:3.11-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir --only-binary=pyswisseph -r requirements.txt
COPY app ./app
# Ephemeris files are not in this repo. Same three files as
# https://github.com/aloistr/swisseph/tree/master/ephe (1800–2400).
ENV EPHE_PATH=/srv/ephe PYTHONUNBUFFERED=1
RUN mkdir -p /srv/ephe && python -c "import pathlib,urllib.request; d=pathlib.Path('/srv/ephe'); base='https://raw.githubusercontent.com/aloistr/swisseph/master/ephe/';\
[urllib.request.urlretrieve(base+n, d/n) for n in ('sepl_18.se1','semo_18.se1','seas_18.se1')]"
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4", "--proxy-headers", "--forwarded-allow-ips", "*"]
