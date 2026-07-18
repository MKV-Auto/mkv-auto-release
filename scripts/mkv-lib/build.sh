#!/bin/bash
# build.sh - Docker image build functions
# Build operations for mkv command

#--------------------------
# Docker Image Building
#--------------------------
build_image() {
  local version="${1:-latest}"
  log "Building Docker image (version: $version)..."
  
  # Delegate to the build-docker script
  if [ -f "${SCRIPT_DIR}/build-docker" ]; then
    "${SCRIPT_DIR}/build-docker" "$version"
  else
    echo "❌ Error: build-docker script not found at ${SCRIPT_DIR}/build-docker"
    exit 1
  fi
}
