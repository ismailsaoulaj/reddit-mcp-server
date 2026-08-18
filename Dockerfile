# Stage 1: Build dependencies
FROM python:3.11-slim AS builder

WORKDIR /app

# Install build dependencies
RUN pip install --no-cache-dir build hatchling

# Copy the project files
COPY pyproject.toml README.md ./
COPY src/ src/

# Build the wheel
RUN python -m build --wheel

# Stage 2: Runtime environment
FROM python:3.11-slim

WORKDIR /app

# Copy the built wheel from the builder stage
COPY --from=builder /app/dist/*.whl ./

# Install the wheel
RUN pip install --no-cache-dir ./*.whl && rm ./*.whl

# Run as an unprivileged user
USER nobody

# The server communicates via standard I/O, so we don't expose ports.
# We set the entrypoint to the CLI command defined in pyproject.toml
ENTRYPOINT ["reddit-mcp"]
