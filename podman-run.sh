#!/bin/bash

# Runs the Core AI & Data toolkit's container with Podman, without compose.
# API_KEY, ADMIN_API_KEY and ALLOWED_MODELS pass through from the shell, as in
# docker-compose.yml.

set -e

IMAGE_NAME="core-ai-data-toolkit"
CONTAINER_NAME="core-ai-data-toolkit"
PORT="8000"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

print_usage() {
    echo "Usage: $0 {build|run|stop|restart|logs|clean|shell}"
    echo ""
    echo "Commands:"
    echo "  build   - Build the container image"
    echo "  run     - Run the container"
    echo "  stop    - Stop the running container"
    echo "  restart - Restart the container"
    echo "  logs    - Show container logs"
    echo "  clean   - Remove container and image"
    echo "  shell   - Open shell in running container"
    echo ""
}

build_image() {
    echo -e "${YELLOW}Building container image...${NC}"
    podman build --build-arg JOB_ENVS="${JOB_ENVS:-}" -t $IMAGE_NAME .
    echo -e "${GREEN}Image built successfully!${NC}"
}

run_container() {
    echo -e "${YELLOW}Starting container...${NC}"
    
    # Check if container already exists
    if podman ps -a --format "{{.Names}}" | grep -q "^${CONTAINER_NAME}$"; then
        echo -e "${YELLOW}Container exists, removing it first...${NC}"
        podman rm -f $CONTAINER_NAME
    fi
    
    podman run -d \
        --name $CONTAINER_NAME \
        -p $PORT:8000 \
        -e MAX_MODELS="${MAX_MODELS:-}" \
        -e MAX_MEMORY_GB="${MAX_MEMORY_GB:-}" \
        -e HOST=0.0.0.0 \
        -e PORT=8000 \
        -e LOG_LEVEL=INFO \
        -e API_KEY="${API_KEY:-}" \
        -e ADMIN_API_KEY="${ADMIN_API_KEY:-}" \
        -e ALLOWED_MODELS="${ALLOWED_MODELS:-}" \
        -v core-ai-data-toolkit-hf-cache:/app/.cache/huggingface \
        -v core-ai-data-toolkit-config:/app/data/config \
        -v core-ai-data-toolkit-collections:/app/data/arcadedb \
        -v core-ai-data-toolkit-jobs:/app/data/jobs \
        -v core-ai-data-toolkit-datasets:/app/data/datasets \
        -v core-ai-data-toolkit-enrichers:/app/data/enrichers \
        -v core-ai-data-toolkit-evidence:/app/data/evidence \
        -v ./data/images:/app/data/images:ro,Z \
        --restart=unless-stopped \
        $IMAGE_NAME
    
    echo -e "${GREEN}Container started!${NC}"
    echo -e "API available at: http://localhost:$PORT"
    echo -e "Health check: http://localhost:$PORT/health"
}

stop_container() {
    echo -e "${YELLOW}Stopping container...${NC}"
    podman stop $CONTAINER_NAME 2>/dev/null || echo -e "${YELLOW}Container not running${NC}"
    echo -e "${GREEN}Container stopped!${NC}"
}

restart_container() {
    stop_container
    run_container
}

show_logs() {
    echo -e "${YELLOW}Showing container logs...${NC}"
    podman logs -f $CONTAINER_NAME
}

clean_all() {
    echo -e "${YELLOW}Cleaning up...${NC}"
    podman stop $CONTAINER_NAME 2>/dev/null || true
    podman rm $CONTAINER_NAME 2>/dev/null || true
    podman rmi $IMAGE_NAME 2>/dev/null || true
    echo -e "${GREEN}Cleanup complete!${NC}"
}

open_shell() {
    echo -e "${YELLOW}Opening shell in container...${NC}"
    podman exec -it $CONTAINER_NAME /bin/bash
}

# Main script logic
case "${1:-}" in
    build)
        build_image
        ;;
    run)
        run_container
        ;;
    stop)
        stop_container
        ;;
    restart)
        restart_container
        ;;
    logs)
        show_logs
        ;;
    clean)
        clean_all
        ;;
    shell)
        open_shell
        ;;
    *)
        print_usage
        exit 1
        ;;
esac
