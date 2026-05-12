FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# pyproject.toml references README.md as the package readme, so hatchling
# refuses to build without it. Keep the COPY list minimal but complete.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

# In compose we override HOST=0.0.0.0; PORT comes from env (.env or compose).
EXPOSE 8765

CMD ["python", "-m", "confluence_mcp"]
