FROM python:3.12-slim

RUN useradd --create-home --uid 10001 dealerflow
WORKDIR /app
COPY pyproject.toml ./
COPY dealerflow ./dealerflow
RUN pip install --no-cache-dir .

USER dealerflow
ENV DF_DATABASE_URL=sqlite:////data/dealerflow.db DF_WEB_HOST=0.0.0.0 DF_WEB_PORT=8000 DF_ENV=production
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"
CMD ["python", "-m", "dealerflow", "web"]
