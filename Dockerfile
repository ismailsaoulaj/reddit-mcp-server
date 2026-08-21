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

# Expose port 8000 for SSE transport in Open WebUI or other remote services
EXPOSE 8000

# Set entrypoint to run the server in SSE mode by default inside the container.
# Binding to 0.0.0.0 is required for Docker port forwarding to work.
ENTRYPOINT ["reddit-mcp", "--transport", "sse", "--host", "0.0.0.0", "--port", "8000"]
