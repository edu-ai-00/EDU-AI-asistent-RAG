#!/usr/bin/env bash
# The operational entry of the S2 return while R2 exists: it runs the UNCHANGED S2 script, but only when R2 is verified NOT active.
#
#   nginx_s2_guard.sh check                     read-only: 0 = R2 is verified not active (an S2 return is safe), 2 = refused
#   nginx_s2_guard.sh smoke | preflight [...]  read-only S2 commands, handed to the S2 script as they are
#   nginx_s2_guard.sh rollback <S2_SHA>        refused with 2 (nothing changed) unless R2 is verified not active; then the S2 script runs
#   nginx_s2_guard.sh deploy   <S2_SHA>        the same
#
# Why: the S2 `rollback` recreates nginx on the S2 definition (edge only) and leaves cloudflared on `tunnel`: exit 0, smoke OK, tunnel down.
# Refused when ANY of these holds: R2 is active (the checkout carries a step: nginx.conf includes the trust file, docker-compose.yml has
# tunnel_trust; the R2 state directory records a step; nginx or cloudflared sit on the R2 wiring) OR its state cannot be verified (a read
# of a file, the state directory or of a container fails, or Docker answers something unexpected). Unverified is never "not active".
# A container that is PROVEN absent (docker inspect fails and the container list, which succeeds, does not hold it) is not a sign of R2:
# the S2 return exists to recreate it. A failed inspect with a failed or contradicting list proves nothing and is refused.
#
# The old command `bash ~/s2-switch.sh rollback ...` is NOT protected by this file: while the R2 candidate exists THIS is the S2 return entry.
# Run it from the deployment checkout, from a copy taken out of the fetched commit:
#     git show <SHA>:scripts/nginx_s2_guard.sh > ~/r2-tools/s2-guard.sh
# It writes nothing (no state directory, no file, no container) and does not change the S2 script or its evidence.
#
# Environment (all optional): S2_SCRIPT=$HOME/s2-switch.sh  R2_STATE_DIR=$HOME/r2-state  R2_NGINX_CONTAINER=edu_ai_nginx  R2_CLOUDFLARED_CONTAINER=cloudflared
# Exit status: 0 verified not active (check) | 2 refused (active, or cannot verify) | whatever the S2 script returns when it runs

set -u
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'
S2_SCRIPT=${S2_SCRIPT:-$HOME/s2-switch.sh}
STATE_DIR=${R2_STATE_DIR:-$HOME/r2-state}
NGINX=${R2_NGINX_CONTAINER:-edu_ai_nginx}
CF=${R2_CLOUDFLARED_CONTAINER:-cloudflared}
TRUST_TARGET=/etc/edu_ai/tunnel/trust.conf
INSPECT_FORMAT='{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}|{{range .Mounts}}{{.Destination}} {{end}}'
ACTIVE=(); UNVERIFIED=(); VERIFIED=()
C_NETS=""; C_MOUNTS=""

on_net() { case " $1 " in *"_$2 "*|*" $2 "*) return 0 ;; esac; return 1; }
dir_ok() { [ -d "$1" ] && [ -r "$1" ] && [ -x "$1" ]; }
read_file() {           # $1 path: prints it; 0 = read, 1 = absent, 2 = there but unreadable
  [ -e "$1" ] || [ -L "$1" ] || return 1
  [ -f "$1" ] && [ -r "$1" ] || return 2
  cat -- "$1" 2>/dev/null || return 2
}
read_container() {      # $1 container: sets C_NETS and C_MOUNTS; 0 = read, 1 = PROVEN absent, 2 = could not be read
  local out names
  if out=$(docker inspect --type container --format "$INSPECT_FORMAT" "$1" 2>/dev/null); then
    case "$out" in *"|"*) ;; *) return 2 ;; esac                          # one answer with the one separator, or nothing is known
    C_NETS=${out%%|*}; C_MOUNTS=${out#*|}
    case "$C_MOUNTS" in *"|"*) return 2 ;; esac
    return 0
  fi
  names=$(docker ps -a --format '{{.Names}}' 2>/dev/null) || return 2     # the list failed too: nothing is proven
  printf '%s\n' "$names" | grep -qxF -- "$1" && return 2                  # it is there, yet could not be read
  return 1
}

collect() {             # fills ACTIVE, UNVERIFIED and VERIFIED
  local conf comp log rc
  conf=$(read_file nginx/conf/nginx.conf); rc=$?
  if [ "$rc" = 2 ] || { [ -e nginx/conf ] && ! dir_ok nginx/conf; }; then UNVERIFIED+=("nginx/conf/nginx.conf cannot be read")
  elif [ "$rc" = 0 ] && printf '%s\n' "$conf" | grep -v '^[[:space:]]*#' | grep -q 'include /etc/edu_ai/tunnel/\*\.conf;'; then
    ACTIVE+=("the checkout carries an R2 step: nginx/conf/nginx.conf includes the trust file")
  else VERIFIED+=("nginx/conf/nginx.conf"); fi
  comp=$(read_file docker-compose.yml); rc=$?
  if [ "$rc" != 0 ]; then UNVERIFIED+=("docker-compose.yml cannot be read")
  elif printf '%s\n' "$comp" | grep -v '^[[:space:]]*#' | grep -q 'tunnel_trust'; then ACTIVE+=("the checkout carries R2 step 2: docker-compose.yml has the tunnel_trust config")
  else VERIFIED+=("docker-compose.yml"); fi
  if [ ! -e "$STATE_DIR" ] && [ ! -L "$STATE_DIR" ]; then VERIFIED+=("no R2 state directory")
  elif ! dir_ok "$STATE_DIR"; then UNVERIFIED+=("the R2 state directory $STATE_DIR cannot be read")
  else
    log=$(read_file "$STATE_DIR/steps.log"); rc=$?
    if [ "$rc" = 2 ]; then UNVERIFIED+=("$STATE_DIR/steps.log cannot be read")
    elif [ -n "$log" ]; then ACTIVE+=("the R2 state directory $STATE_DIR records applied steps: $(printf '%s\n' "$log" | cut -d' ' -f1 | sort -u | tr '\n' ' ')")
    else VERIFIED+=("R2 state directory without recorded steps"); fi
  fi
  read_container "$NGINX"; rc=$?
  case "$rc" in
    0) if on_net "$C_NETS" tunnel; then ACTIVE+=("container $NGINX is on the tunnel network"); fi
       case " $C_MOUNTS " in *" $TRUST_TARGET "*) ACTIVE+=("container $NGINX mounts the trust file") ;; esac
       VERIFIED+=("container $NGINX read") ;;
    1) VERIFIED+=("container $NGINX proven absent (the S2 return recreates it)") ;;
    *) UNVERIFIED+=("container $NGINX could not be read (docker inspect failed or answered something unexpected, and the container list does not prove it absent)") ;;
  esac
  read_container "$CF"; rc=$?
  case "$rc" in
    0) if on_net "$C_NETS" tunnel || on_net "$C_NETS" tunnel_egress; then ACTIVE+=("container $CF is on the tunnel networks")
       elif ! on_net "$C_NETS" edge; then ACTIVE+=("container $CF is not on edge (the S2 wiring has it there)"); fi
       VERIFIED+=("container $CF read") ;;
    1) VERIFIED+=("container $CF proven absent") ;;
    *) UNVERIFIED+=("container $CF could not be read (docker inspect failed or answered something unexpected, and the container list does not prove it absent)") ;;
  esac
}

refuse_unless_not_active() {
  [ -f docker-compose.yml ] || { echo "REFUSED: run this from the deployment checkout (no docker-compose.yml here)"; exit 2; }
  collect
  if [ "${#ACTIVE[@]}" -gt 0 ]; then
    echo "REFUSED: R2 is active, an S2 return now leaves nginx on the S2 definition and cloudflared on tunnel (exit 0, tunnel down). Nothing was changed."
    printf '  - %s\n' "${ACTIVE[@]}"
  fi
  if [ "${#UNVERIFIED[@]}" -gt 0 ]; then
    echo "REFUSED: cannot verify that R2 is not active, so the S2 return is not started. Nothing was changed."
    printf '  - %s\n' "${UNVERIFIED[@]}"
  fi
  if [ "${#ACTIVE[@]}" -gt 0 ]; then echo "Return R2 completely first: bash r2-switch.sh rollback-all (expect R2_RESULT status=returned), then run this again."; fi
  if [ "${#UNVERIFIED[@]}" -gt 0 ]; then echo "Fix the read that failed (Docker reachable? permissions? run as the user of R2) and run this again."; fi
  [ "${#ACTIVE[@]}" -eq 0 ] && [ "${#UNVERIFIED[@]}" -eq 0 ] || exit 2
}

cmd=${1:-}
case "$cmd" in
  check) refuse_unless_not_active; echo "R2 is verified not active ($(printf '%s; ' "${VERIFIED[@]}")): an S2 return does not collide with it."; exit 0 ;;
  smoke|preflight) ;;
  rollback|deploy) refuse_unless_not_active ;;
  *) sed -n '2,9p' "$0"; exit 2 ;;
esac
[ -r "$S2_SCRIPT" ] || { echo "REFUSED: the S2 script $S2_SCRIPT is not readable (S2_SCRIPT=...)"; exit 2; }
sha=${2:-}
if [ -n "$sha" ] && want=$(git rev-parse --verify --quiet "$sha:scripts/nginx_s2_switch.sh") && [ "$want" = "$(git hash-object "$S2_SCRIPT" 2>/dev/null)" ]; then
  echo "the S2 script is the committed original of $sha (blob $want); running it unchanged"
else
  echo "NOTE: the S2 script could not be compared with a committed original; running it as it is"
fi
exec bash "$S2_SCRIPT" "$@"
