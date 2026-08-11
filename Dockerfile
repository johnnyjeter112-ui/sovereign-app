FROM python:3.11-slim

# Create workdir
WORKDIR /app

# Copy app
COPY . /app

# Expose port (metadata)
EXPOSE 8080

# Use PORT env var provided by host (Render/Heroku style). Default 8080.
ENV PORT=8080

# Run the server
CMD ["python3", "server.py"]
