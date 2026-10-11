#!/usr/bin/env bash
# Prepare a new Ubuntu x86_64 host. EDU-AI itself runs as the invoking user.
# Docker repository: https://docs.docker.com/engine/install/ubuntu/
set -euo pipefail

fail() { printf 'REFUSED: %s\n' "$*" >&2; exit 2; }
installed() { [[ $(dpkg-query -W -f='${Status}' "$1" 2>/dev/null || true) == 'install ok installed' ]]; }
read_platform() {
    [[ -r /etc/os-release ]] || fail 'Cannot read /etc/os-release.'
    # shellcheck disable=SC1091
    . /etc/os-release
}

prepare_sudo() {
    command -v sudo >/dev/null || fail 'sudo is required.'
    SUDO=(sudo)
    # Validate an actual command: sudo-rs -v can demand authentication even
    # for a NOPASSWD user. This supports both NOPASSWD and a terminal prompt.
    "${SUDO[@]}" true
}

main() {
    local mode=check target_user='' target_uid arg package codename tmp docker_present=0
    while (($#)); do
        arg=$1; shift
        case "$arg" in
            --check) mode=check ;;
            --apply) mode=apply ;;
            --docker-user) (($#)) || fail '--docker-user needs a user name.'; target_user=$1; shift ;;
            --help|-h)
                printf '%s\n' \
                    'Usage: bash scripts/prepare_ubuntu.sh [--check | --apply] [--docker-user USER]' \
                    'Default: read-only check. --apply installs missing host packages using sudo.' \
                    'Adds the invoking user to the docker group (root-equivalent access); reconnect afterward.' \
                    'From root, --docker-user must name the non-root installation user.'
                return 0 ;;
            *) fail "Unknown argument: $arg" ;;
        esac
    done
    read_platform
    [[ ${ID:-} == ubuntu && $(uname -m) == x86_64 ]] || fail 'Requires Ubuntu x86_64.'
    case ${VERSION_ID:-} in 22.04|24.04|26.04) ;; *) fail 'Requires Ubuntu 22.04, 24.04 or 26.04.' ;; esac
    codename=${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}
    [[ $codename =~ ^[a-z]+$ ]] || fail 'Ubuntu codename is missing or invalid.'
    [[ ${DOCKER_HOST:-} == '' && ${DOCKER_CONTEXT:-default} == default ]] || fail 'Use the local default Docker daemon; unset DOCKER_HOST/DOCKER_CONTEXT.'
    command -v dpkg-query >/dev/null || fail 'dpkg-query is missing.'
    for package in docker.io docker-compose docker-compose-v2 docker-doc docker-buildx podman-docker containerd runc; do
        installed "$package" && fail "Conflicting package $package is installed; no packages were removed."
    done
    if command -v docker >/dev/null; then
        installed docker-ce && installed docker-ce-cli && installed containerd.io || fail 'Existing Docker is not a complete Docker CE apt installation.'
        docker_present=1
    else
        # Preserve any pre-existing repository configuration for manual review.
        for arg in /etc/apt/sources.list.d/docker.sources /etc/apt/sources.list.d/docker.list /etc/apt/keyrings/docker.asc; do
            [[ ! -e $arg && ! -L $arg ]] || fail "Existing Docker repository file: $arg."
        done
    fi
    printf 'HOST: Ubuntu %s, x86_64\n' "$VERSION_ID"
    printf '%s\n' 'Host packages: ca-certificates curl git python3 iproute2 openssl.'
    if ((docker_present)); then
        printf '%s\n' 'Docker CE already installed: its version will be kept; missing Compose/Buildx plugins will be installed.'
    else
        printf '%s\n' 'Docker packages: docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin (official stable apt repository).'
    fi
    [[ $mode == apply ]] || { printf '%s\n' 'CHECK ONLY: nothing changed. Run with --apply to install.'; return 0; }

    if [[ -z $target_user ]]; then
        if ((EUID == 0)); then target_user=${SUDO_USER:-}; else target_user=$(id -un); fi
    fi
    [[ $target_user =~ ^[a-z_][a-z0-9_-]*[$]?$ ]] || fail 'Specify a valid non-root --docker-user.'
    target_uid=$(id -u "$target_user") || fail 'The installation user does not exist.'
    [[ $target_uid != 0 ]] || fail 'EDU-AI must run under a non-root user.'
    if ((EUID == 0)); then SUDO=(); else prepare_sudo; fi
    if ((docker_present)); then
        [[ $("${SUDO[@]}" docker --context default info --format '{{.Containers}}') == 0 ]] || fail 'Docker has containers or cannot be inspected; this is not a clean host.'
    else
        # Refuse stale engine state too; never remove existing containers or data.
        "${SUDO[@]}" test ! -d /var/lib/docker/containers || fail 'Existing Docker state requires manual review.'
    fi

    local base_packages=() docker_packages=()
    for package in ca-certificates curl git python3 iproute2 openssl; do
        installed "$package" || base_packages+=("$package")
    done
    if ((${#base_packages[@]})); then
        "${SUDO[@]}" apt-get update
        "${SUDO[@]}" apt-get install -y --no-install-recommends "${base_packages[@]}"
    fi
    if ((!docker_present)); then
        tmp=$(mktemp -d)
        prepare_tmp=$tmp
        trap 'rm -f -- "$prepare_tmp/docker.asc" "$prepare_tmp/docker.sources"; rmdir -- "$prepare_tmp"' EXIT
        curl --fail --silent --show-error --location --proto '=https' \
            https://download.docker.com/linux/ubuntu/gpg -o "$tmp/docker.asc"
        printf '%s\n' 'Types: deb' 'URIs: https://download.docker.com/linux/ubuntu' \
            "Suites: $codename" 'Components: stable' 'Architectures: amd64' \
            'Signed-By: /etc/apt/keyrings/docker.asc' > "$tmp/docker.sources"
        "${SUDO[@]}" install -d -m 0755 /etc/apt/keyrings
        "${SUDO[@]}" install -m 0644 "$tmp/docker.asc" /etc/apt/keyrings/docker.asc
        "${SUDO[@]}" install -m 0644 "$tmp/docker.sources" /etc/apt/sources.list.d/docker.sources
        docker_packages=(docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin)
    else
        for package in docker-buildx-plugin docker-compose-plugin; do
            installed "$package" || docker_packages+=("$package")
        done
    fi
    if ((docker_present && ${#docker_packages[@]})); then
        # Pin the already-installed engine packages while adding plugins.
        for package in docker-ce docker-ce-cli containerd.io; do
            docker_packages+=("$package=$(dpkg-query -W -f='${Version}' "$package")")
        done
    fi
    if ((${#docker_packages[@]})); then
        "${SUDO[@]}" apt-get update
        "${SUDO[@]}" apt-get install -y --no-install-recommends "${docker_packages[@]}"
    fi
    "${SUDO[@]}" systemctl enable --now docker
    "${SUDO[@]}" usermod -aG docker "$target_user"
    "${SUDO[@]}" docker --context default info --format 'Docker Engine: {{.ServerVersion}} ({{.OSType}}/{{.Architecture}})'
    "${SUDO[@]}" docker compose version
    "${SUDO[@]}" docker buildx version
    printf 'READY: reconnect as %s, then run docker info and docker compose version without sudo.\n' "$target_user"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi
