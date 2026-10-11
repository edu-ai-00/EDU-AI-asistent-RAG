#!/usr/bin/env bash
# Switch ONLY the nginx service between its pre-S2 and its S2 definition, with checks before and after.
#
#   nginx_s2_switch.sh smoke                the smoke list against the running nginx (read-only)
#   nginx_s2_switch.sh preflight <S2_SHA>   validates the candidate, changes nothing
#   nginx_s2_switch.sh deploy    <S2_SHA>   preflight, fast-forward merge, recreate nginx, health and smoke
#                                           deadlines, automatic return (revert of S2_SHA only) if they fail
#   nginx_s2_switch.sh rollback  <S2_SHA>   return to the pre-S2 nginx: `git revert S2_SHA`; the nginx service
#                                           and the old mount sources are put back in place if git cannot do it
#
# Run it from the deployment checkout (cwd), from a COPY taken out of the fetched S2 commit
# (`git show <S2_SHA>:scripts/nginx_s2_switch.sh > ~/s2-switch.sh`), never from the tree it is about to
# change. It never runs reset, clean, stash, path checkout or a force push, and it touches no other service.
#
# The candidate is validated against the EFFECTIVE Compose model: `docker compose config` with exactly the
# arguments, environment and override files that `up` will use, only the base file replaced by the candidate's.
# Compose is told where the base file goes by the line `@BASE@` in S2_COMPOSE_ARGS_FILE (`-f` / `@BASE@`).
# Without that file the checkout's `.env` (or the environment) must pin COMPOSE_FILE=docker-compose.yml.
#
# The nginx container is created from an image ID, never from a tag: the validated ID for the deploy, the ID captured before the switch
# for the return. Both are saved in $S2_STATE_DIR/images.env (keep that directory until the return window ends: a later `rollback` in a
# new process reads it) and kept alive by private tags `$S2_EVIDENCE_REPO:original-<id>` / `validated-<id>`.
#
# If the first `up` of the return fails or leaves no running container (a killed `up` can leave a half-made one), the nginx containers of the
# project are removed (each one only after its labels show the expected Compose project and the service nginx) and `up` is tried once more:
# nginx is stateless. A container of another project or service that holds the name is a NAME COLLISION: it is kept and the return fails (exit 5).
#
# Exit status: 0 ok | 2 refused before any change | 3 failed, left as is (S2_AUTO_ROLLBACK=0)
#              4 failed and returned to the pre-S2 nginx | 5 the return itself failed | 99 test hook
#
# Environment (all optional):
#   S2_CONTAINER=edu_ai_nginx       S2_STATE_DIR=$HOME/s2-state     saved copies, log, lists, pid
#   S2_HEALTH_DEADLINE=90  S2_SMOKE_DEADLINE=30  S2_UP_DEADLINE=120  S2_RETURN_UP_DEADLINE=120   seconds
#   S2_COMPOSE_ARGS_FILE=FILE       one `docker compose` argument per line, `@BASE@` marks the base compose file
#   S2_SMOKE_LIST="port|path|status[,status]" lines (default: the production list below)
#   S2_AUTO_ROLLBACK=1             S2_EVIDENCE_REPO=s2-evidence/nginx   (repository name of the private tags that keep the images)
#   test hooks: S2_TEST_STOP_AFTER=<phase> (exit 99)   S2_TEST_SLEEP_AFTER=<phase>:<seconds>[,<phase>:<seconds>...]
#               phases: preflight, merged, recreated, returning   S2_SOURCE_ONLY=1 (load the functions, run nothing)

set -u
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

CONTAINER=${S2_CONTAINER:-edu_ai_nginx}
STATE_DIR=${S2_STATE_DIR:-$HOME/s2-state}
HEALTH_DEADLINE=${S2_HEALTH_DEADLINE:-90}     # first check at ~15 s; "unhealthy" needs 3 failed checks: ~50 s
SMOKE_DEADLINE=${S2_SMOKE_DEADLINE:-30}
UP_DEADLINE=${S2_UP_DEADLINE:-120}
RETURN_UP_DEADLINE=${S2_RETURN_UP_DEADLINE:-120}   # the return has its own deadline: a short UP_DEADLINE must not break it
AUTO_ROLLBACK=${S2_AUTO_ROLLBACK:-1}
BASE_MARK='@BASE@'
COMPOSE_ARGS=()
# the files the PREVIOUS nginx definition mounts; they must exist and stay as they were
OLD_FILES=(nginx/nginx.conf nginx/search_api.conf)
# Every request is answered from inside the nginx container (nothing is published or sent outside).
# Statuses come from nginx itself (/health, the 404 envelopes) or from the upstreams' own health endpoints;
# the list was read from production on 2026-10-02 (statuses only): pgAdmin answers with a redirect to its login.
SMOKE_LIST=${S2_SMOKE_LIST:-'80|/health|200
80|/api/health|200
80|/search/health|200
80|/chat/health|200
80|/qa-api/v1/health|200
80|/search/v1/query-log|404
81|/search/health|200
81|/qa/|404
81|/api/|404
8443|/pgadmin/|200,301,302
8444|/health|200'}

mkdir -p "$STATE_DIR" || { echo "cannot create $STATE_DIR" >&2; exit 2; }
LOG="$STATE_DIR/log"
TMP=""
CAND=""; PRE=""; BASE=""; IMAGE=""; IMG_REF=""; IMG_ID=""; CAND_NGINX_HASH=""
trap '[ -z "$TMP" ] || rm -rf "$TMP"' EXIT

log()  { printf '%s %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$LOG"; }
die()  { local code=$1; shift; log "FAIL: $*"; exit "$code"; }
now()  { date +%s.%N; }
secs() { awk -v a="$1" -v b="$2" 'BEGIN { printf "%.1f", b - a }'; }
abs()  { ( cd "$1" && { pwd -W 2>/dev/null || pwd; } ); }
mk_tmp() { [ -n "$TMP" ] || TMP=$(mktemp -d); }

# ── how Compose is called: ONE definition for validation and for `up` ──────────────────────
load_compose_args() {
  COMPOSE_ARGS=()
  [ -n "${S2_COMPOSE_ARGS_FILE:-}" ] || return 0
  [ -r "$S2_COMPOSE_ARGS_FILE" ] || die 2 "S2_COMPOSE_ARGS_FILE '$S2_COMPOSE_ARGS_FILE' is not readable: refusing to fall back to the defaults"
  mapfile -t COMPOSE_ARGS < "$S2_COMPOSE_ARGS_FILE"
  COMPOSE_ARGS=("${COMPOSE_ARGS[@]%$'\r'}")
  [ "${#COMPOSE_ARGS[@]}" -gt 0 ] || die 2 "S2_COMPOSE_ARGS_FILE '$S2_COMPOSE_ARGS_FILE' is empty"
  local n=0 x
  for x in "${COMPOSE_ARGS[@]}"; do [ "$x" = "$BASE_MARK" ] && n=$((n + 1)); done
  [ "$n" = 1 ] || die 2 "S2_COMPOSE_ARGS_FILE must contain the line $BASE_MARK exactly once (it marks the base compose file), found $n"
}

# without an arguments file the checkout's own .env decides which files Compose reads; production pins it
check_default_mode() {
  [ "${#COMPOSE_ARGS[@]}" -eq 0 ] || return 0
  local cf=${COMPOSE_FILE:-}
  if [ -z "$cf" ] && [ -f .env ]; then
    cf=$(grep -E '^COMPOSE_FILE=' .env | tail -1 | cut -d= -f2- | tr -d "\"' " | tr -d '\r')
  fi
  [ "$cf" = docker-compose.yml ] || die 2 "without S2_COMPOSE_ARGS_FILE the environment or .env must pin COMPOSE_FILE=docker-compose.yml (found '${cf:-nothing}'); otherwise the files validated here are not the files up reads"
}

build_args() {          # $1 = what stands for the base file: its path, or - for stdin; result in A
  A=(); local x
  for x in "${COMPOSE_ARGS[@]+"${COMPOSE_ARGS[@]}"}"; do
    if [ "$x" = "$BASE_MARK" ]; then A+=("$1"); else A+=("$x"); fi
  done
}
base_path() { printf '%s/docker-compose.yml' "$(abs .)"; }
compose()   { build_args "$(base_path)"; docker compose "${A[@]+"${A[@]}"}" "$@"; }

# ── the image is referenced by ID, never by a tag, whenever the script creates the nginx container ──
# A tag can be moved (a pull, a retag) between the validation and the recreate, and again before a return. The pin is an
# override file, last in the file list, that sets the nginx image to a concrete image ID; `up` and the return use it.
PIN_ID=""
pin_path()  { printf '%s/image-pin.yaml' "$(abs "$STATE_DIR")"; }
write_pin() {
  printf 'services:\n  nginx:\n    image: %s\n' "$1" > "$STATE_DIR/image-pin.yaml" || return 1
  PIN_ID=$1
}
compose_up() {          # $1 = deadline in seconds, the rest are the `up` arguments; runs on the pinned image ID when there is a pin
  local d=$1; shift
  build_args "$(base_path)"
  if [ -n "$PIN_ID" ]; then
    if [ "${#COMPOSE_ARGS[@]}" -eq 0 ]; then A=(-f "$(base_path)"); fi      # default mode reads exactly COMPOSE_FILE=docker-compose.yml
    A+=(-f "$(pin_path)")
  fi
  timeout "$d" docker compose "${A[@]+"${A[@]}"}" "$@"
}

up_logged() {           # compose_up with its output appended to the log; the last lines are shown when it fails
  local out rc
  out=$(compose_up "$@" 2>&1); rc=$?
  printf '%s\n' "$out" >> "$LOG"
  [ "$rc" = 0 ] || log "docker compose said (rc=$rc): $(printf '%s' "$out" | tail -4 | tr '\n' ' ' | cut -c1-500)"
  return "$rc"
}

# The evidence that a LATER process needs to return without any help from the outside: the image before the switch, the image
# that was validated, the candidate. Kept in the state directory until the owner deletes it.
EVIDENCE_REPO=${S2_EVIDENCE_REPO:-s2-evidence/nginx}
IMAGES_ENV="$STATE_DIR/images.env"
evidence_get() { sed -n "s/^$1=//p" "$IMAGES_ENV" 2>/dev/null | head -1; }
save_image_evidence() {  # original (running before the switch) and validated image IDs
  local orig=$IMAGE
  if [ -f "$IMAGES_ENV" ] && [ "$(evidence_get CANDIDATE)" = "$CAND" ] && [ -n "$(evidence_get ORIGINAL_IMAGE_ID)" ]; then
    orig=$(evidence_get ORIGINAL_IMAGE_ID)       # a re-run of the same deploy must not replace the image of the very first run
  fi
  docker image inspect "$orig" > /dev/null 2>&1 || die 2 "the original image $orig is not present locally"
  # private tags keep both images alive (an untagged image can be pruned) whatever happens to the public tag
  docker tag "$orig" "$EVIDENCE_REPO:original-${orig#sha256:}" 2>/dev/null || die 2 "cannot tag the original image $orig"
  docker tag "$IMG_ID" "$EVIDENCE_REPO:validated-${IMG_ID#sha256:}" 2>/dev/null || die 2 "cannot tag the validated image $IMG_ID"
  printf 'ORIGINAL_IMAGE_ID=%s\nVALIDATED_IMAGE_ID=%s\nIMAGE_REF=%s\nCANDIDATE=%s\nPRE=%s\nCONTAINER=%s\n' \
    "$orig" "$IMG_ID" "$IMG_REF" "$CAND" "$PRE" "$CONTAINER" > "$IMAGES_ENV.new" && mv "$IMAGES_ENV.new" "$IMAGES_ENV" || die 2 "cannot write $IMAGES_ENV"
  log "image evidence saved in $IMAGES_ENV: original $orig, validated $IMG_ID"
}
# the same arguments, environment and override files, but THIS file is the base (read from stdin)
compose_with_base() {
  local f=$1; shift
  if [ "${#COMPOSE_ARGS[@]}" -gt 0 ]; then
    build_args -; docker compose "${A[@]}" "$@" < "$f"
  else
    docker compose -f - "$@" < "$f"
  fi
}
project_dir() {
  local i
  for i in "${!COMPOSE_ARGS[@]}"; do
    if [ "${COMPOSE_ARGS[$i]}" = --project-directory ]; then printf '%s' "${COMPOSE_ARGS[$((i + 1))]}"; return; fi
  done
  abs .
}

phase() {
  echo "$1" > "$STATE_DIR/phase"; log "phase: $1"
  local h hooks=()
  IFS=, read -ra hooks <<< "${S2_TEST_SLEEP_AFTER:-}"
  for h in "${hooks[@]+"${hooks[@]}"}"; do [ "${h%%:*}" = "$1" ] && sleep "${h#*:}"; done
  if [ "${S2_TEST_STOP_AFTER:-}" = "$1" ]; then log "TEST HOOK: stopping after $1"; exit 99; fi
  return 0
}

# ── observation helpers: a failed probe is ERR, never a status ────────────────────────────
cstate() { docker inspect --format '{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$CONTAINER" 2>/dev/null; }

http_status() {      # port path -> status of the FIRST response (a redirect is reported, then followed by wget), or ERR
  local out st
  out=$(docker exec "$CONTAINER" wget -S -O /dev/null -T 5 "http://127.0.0.1:$1$2" 2>&1) || true
  st=$(printf '%s\n' "$out" | grep -E 'HTTP/[0-9.]+ [0-9]{3}' | head -1 | sed -E 's/.*HTTP\/[0-9.]+ ([0-9]{3}).*/\1/')
  if [ -n "$st" ]; then echo "$st"; else echo ERR; fi
}

wait_ready() {       # $1 = 1: the S2 definition must have a healthcheck and report healthy; 0: running and /health 200
  local deadline=$(( $(date +%s) + HEALTH_DEADLINE )) st n
  while :; do
    if ! st=$(cstate); then log "container $CONTAINER does not exist"; return 1; fi
    if [ "$1" = 1 ]; then      # a NEW container that was restarted is crash-looping, however its health looks right now
      n=$(docker inspect --format '{{.RestartCount}}' "$CONTAINER" 2>/dev/null || echo 0)
      [ "${n:-0}" -gt 0 ] && { log "the new container was restarted ${n}x (crash loop), state $st"; return 1; }
    fi
    case "$st" in
      "running|healthy") return 0 ;;
      "running|unhealthy") log "container is unhealthy"; return 1 ;;
      "running|starting") ;;
      "running|none")
        if [ "$1" = 1 ]; then log "container has no healthcheck (the S2 definition has one)"; return 1; fi
        [ "$(http_status 80 /health)" = 200 ] && return 0 ;;
      restarting\|*) [ "$1" = 1 ] && { log "container state $st"; return 1; } ;;   # during the return it may still be coming back
      *) log "container state $st"; return 1 ;;
    esac
    if [ "$(date +%s)" -ge "$deadline" ]; then log "not ready within ${HEALTH_DEADLINE}s (state $st)"; return 1; fi
    sleep 1
  done
}

smoke_once() {       # prints the failures and returns 1, or returns 0 when every listed request has an accepted status
  local bad="" port path want got
  while IFS='|' read -r port path want; do
    [ -z "$port" ] && continue
    got=$(http_status "$port" "$path")
    case ",$want," in *",$got,"*) ;; *) bad="$bad $port$path(want $want, got $got)" ;; esac
  done <<< "$SMOKE_LIST"
  [ -z "$bad" ] && return 0
  printf '%s' "$bad"; return 1
}

smoke() {            # the whole list, retried until the deadline
  local deadline=$(( $(date +%s) + SMOKE_DEADLINE )) bad n
  n=$(printf '%s\n' "$SMOKE_LIST" | grep -c .)
  while :; do
    if bad=$(smoke_once); then log "smoke OK ($n requests)"; return 0; fi
    if [ "$(date +%s)" -ge "$deadline" ]; then log "smoke failed after ${SMOKE_DEADLINE}s:$bad"; return 1; fi
    sleep 1
  done
}

project_ids() { compose ps -a -q 2>/dev/null | xargs -r docker inspect --format '{{.Name}} {{.Id}}' 2>/dev/null | sort; }

# ── the effective Compose model ────────────────────────────────────────────────────────────
# `config --hash '*'` of the model whose base file is $1, sorted, into $2. Fails when Compose fails or prints nothing.
eff_hash_file() {
  local out
  out=$(compose_with_base "$1" config --hash '*' 2>/dev/null) || return 1
  [ -n "$out" ] || return 1
  printf '%s\n' "$out" | sort > "$2"
}

# The hash of ONE service, found by its exact name (lines are `<service> <hash>`). Fails, printing nothing, unless there is
# exactly one line for the service and its hash is not empty.
svc_hash() {
  local n h
  n=$(awk -v s="$2" '$1 == s' "$1" | grep -c .)
  [ "$n" = 1 ] || return 1
  h=$(awk -v s="$2" '$1 == s && NF == 2 { print $2 }' "$1")
  [ -n "$h" ] || return 1
  printf '%s' "$h"
}

nginx_block_stdin() { awk '/^  nginx:$/ { f = 1; next } f && /^  [^ ]/ { f = 0 } f { print }'; }
# The nginx service of an effective model; the rest of the model (it can hold secrets) never touches the disk.
# $1 = base compose file for the candidate ('' = the checkout's own), $2 = output file; Compose's stderr goes to $TMP/model.err.
model_nginx() {
  local rc
  if [ -n "$1" ]; then
    compose_with_base "$1" config 2> "$TMP/model.err" | nginx_block_stdin > "$2"; rc=${PIPESTATUS[0]}
  else
    compose config 2> "$TMP/model.err" | nginx_block_stdin > "$2"; rc=${PIPESTATUS[0]}
  fi
  [ "$rc" = 0 ] && [ -s "$2" ]
}
nginx_section() { awk -v k="$2:" '/^    [^ ]/ { on = ($1 == k) } on' "$1"; }           # one key of the nginx service, with its children
strip_s2_keys() { awk '/^    [^ ]/ { skip = ($1 == "depends_on:" || $1 == "healthcheck:" || $1 == "volumes:") } !skip { print }'; }
norm_path()     { local p=${1//\\//}; p=${p%/}; case "${OSTYPE:-}" in msys*|cygwin*) p=${p,,} ;; esac; printf '%s' "$p"; }

# the top-level keys of the nginx service whose content differs between two (stripped) service blocks
differing_keys() {
  local k keys out=""
  keys=$(cat "$1" "$2" | awk '/^    [^ ]/ { print $1 }' | sort -u)
  for k in $keys; do
    [ "$(nginx_section "$1" "${k%:}")" = "$(nginx_section "$2" "${k%:}")" ] || out="$out ${k%:}"
  done
  printf '%s' "$out"
}
# The candidate against the CURRENT model, both through the same Compose arguments, environment and overrides.
# Sets IMG_REF, IMG_ID, CAND_NGINX_HASH. Refuses (exit 2) on anything but the S2 change of the nginx service.
effective_model_check() {
  mk_tmp
  model_nginx "" "$TMP/cur.nginx" \
    || die 2 "docker compose config fails (or has no nginx service) for the CURRENT checkout with the arguments that up uses: $(head -c 300 "$TMP/model.err" | tr '\n' ' ')"
  model_nginx "$TMP/cand/docker-compose.yml" "$TMP/cand.nginx" \
    || die 2 "docker compose config fails (or has no nginx service) for the CANDIDATE with the arguments that up uses: $(head -c 300 "$TMP/model.err" | tr '\n' ' ')"
  local out
  out=$(compose config --hash '*' 2>/dev/null) && [ -n "$out" ] || die 2 "docker compose config --hash fails for the current checkout"
  printf '%s\n' "$out" | sort > "$TMP/cur.hash"
  eff_hash_file "$TMP/cand/docker-compose.yml" "$TMP/cand.hash" || die 2 "docker compose config --hash fails for the candidate"
  svc_hash "$TMP/cur.hash" nginx > /dev/null || die 2 "the current model has no single non-empty nginx hash"
  CAND_NGINX_HASH=$(svc_hash "$TMP/cand.hash" nginx) || die 2 "the candidate model has no single non-empty nginx hash"
  [ "$(awk '{print $1}' "$TMP/cur.hash")" = "$(awk '{print $1}' "$TMP/cand.hash")" ] || die 2 "the set of services differs"
  local changed; changed=$(diff "$TMP/cur.hash" "$TMP/cand.hash" | sed -n 's/^> \([^ ]*\) .*/\1/p' | tr '\n' ' ')
  if [ "$PRE" = "$CAND" ]; then
    [ -z "$changed" ] || die 2 "HEAD is already the candidate but the effective model of $changed differs from it"
  else
    [ "$changed" = "nginx " ] || die 2 "the effective model changes services other than nginx: ${changed:-none (no change at all?)}"
  fi
  strip_s2_keys < "$TMP/cur.nginx" > "$TMP/cur.rest"; strip_s2_keys < "$TMP/cand.nginx" > "$TMP/cand.rest"
  cmp -s "$TMP/cur.rest" "$TMP/cand.rest" \
    || die 2 "the nginx service differs beyond volumes, depends_on and healthcheck, in: $(differing_keys "$TMP/cur.rest" "$TMP/cand.rest")"
  # what S2 is allowed to change must be exactly the S2 definition
  [ -z "$(nginx_section "$TMP/cand.nginx" depends_on)" ] || die 2 "the candidate nginx still has depends_on"
  local vol hc src want
  vol=$(nginx_section "$TMP/cand.nginx" volumes)
  [ "$(printf '%s\n' "$vol" | grep -c '^      - ')" = 1 ] || die 2 "the candidate nginx must have exactly one mount"
  printf '%s\n' "$vol" | grep -q '^      - type: bind$' || die 2 "the candidate mount is not a bind mount"
  printf '%s\n' "$vol" | grep -qx '        target: /etc/nginx' || die 2 "the candidate mount target is not /etc/nginx"
  printf '%s\n' "$vol" | grep -qx '        read_only: true' || die 2 "the candidate mount is not read-only"
  src=$(printf '%s\n' "$vol" | sed -n 's/^        source: //p' | tr -d "\"'")
  want="$(project_dir)/nginx/conf"
  [ "$(norm_path "$src")" = "$(norm_path "$want")" ] || die 2 "the candidate mount source is '$src', expected '$want' (nothing but nginx/conf may be mounted)"
  hc=$(nginx_section "$TMP/cand.nginx" healthcheck)
  [ -n "$hc" ] || die 2 "the candidate nginx has no healthcheck"
  for want in '        - wget' '        - http://127.0.0.1/health' '      interval: 15s' '      timeout: 3s' '      retries: 3' '      start_period: 5s'; do
    printf '%s\n' "$hc" | grep -qxF -- "$want" || die 2 "the candidate healthcheck lacks '${want//  /}'"
  done
  printf '%s\n' "$hc" | grep -q 'disable' && die 2 "the candidate healthcheck is disabled"
  # the image the new container will use must be the one that is running and the one nginx -t runs on
  IMG_REF=$(awk '/^    image:/ { print $2; exit }' "$TMP/cand.nginx" | tr -d "\"'")
  [ -n "$IMG_REF" ] || die 2 "the candidate nginx has no image"
  IMG_ID=$(docker image inspect --format '{{.Id}}' "$IMG_REF" 2>/dev/null) \
    || die 2 "image $IMG_REF is not present locally; the deploy never pulls (--pull never)"
  [ "$IMG_ID" = "$IMAGE" ] || die 2 "image $IMG_REF is $IMG_ID but the running container uses $IMAGE (changed image or moved tag); S2 changes no image"
}

# ── preflight: everything is checked BEFORE the checkout or the container is touched ──────
preflight() {
  CAND=$(git rev-parse --verify --quiet "$1^{commit}") || die 2 "candidate $1 is not a commit in this checkout (git fetch first)"
  PRE=$(git rev-parse HEAD)
  git merge-base --is-ancestor "$PRE" "$CAND" || die 2 "HEAD is not an ancestor of the candidate: no fast-forward"
  [ "$(git rev-list --parents -n1 "$CAND" | wc -w)" -eq 2 ] || die 2 "the candidate is a merge commit; it must be the single S2 commit"
  BASE=$PRE; [ "$PRE" = "$CAND" ] && BASE=$(git rev-parse "$CAND^")
  git diff --quiet "$BASE" "$CAND^" -- docker-compose.yml "${OLD_FILES[@]}" || die 2 "commits before S2 change the nginx definition: this is not an S2-only deployment"
  # a local change matters where the merge writes: the file would block it, or the validated version would not be the one that runs
  local f dirty=""
  for f in $(git diff --name-only HEAD); do
    git diff --quiet "$PRE" "$CAND" -- "$f" || dirty="$dirty $f"
  done
  [ -z "$dirty" ] || die 2 "tracked files that the merge changes are modified locally:$dirty"
  if [ "$PRE" != "$CAND" ]; then
    for f in $(git diff --name-only --diff-filter=A "$PRE" "$CAND"); do
      [ -e "$f" ] && die 2 "$f exists in the checkout but is added by the candidate"
    done
  fi
  local st; st=$(cstate) || die 2 "container $CONTAINER not found"
  case "$st" in running\|*) ;; *) die 2 "container $CONTAINER is $st" ;; esac
  IMAGE=$(docker inspect --format '{{.Image}}' "$CONTAINER")
  mk_tmp; mkdir -p "$TMP/cand"
  git archive "$CAND" docker-compose.yml nginx/conf | tar -x -C "$TMP/cand" || die 2 "cannot extract the candidate"
  # 1. the effective model, through the same Compose arguments, environment and override files as up
  effective_model_check
  # 2. nginx -t on the candidate files, in exactly the image that was just resolved (by ID), no network, no upstreams
  # nginx is started by the image as `nginx -g 'daemon off;'`: the test must pass the same global directive, or a `daemon off;` in the
  # file (a duplicate at start) passes the test and then crash-loops the container
  local out gflag=()
  case "$(docker image inspect --format '{{json .Config.Cmd}}' "$IMG_ID" 2>/dev/null)" in *'daemon off;'*) gflag=(-g 'daemon off;') ;; esac
  out=$(docker run --rm --network none --entrypoint nginx --mount "type=bind,source=$(abs "$TMP/cand/nginx/conf"),target=/etc/nginx,readonly" "$IMG_ID" -t "${gflag[@]+"${gflag[@]}"}" 2>&1) \
    || die 2 "nginx -t failed on the candidate: $out"
  printf '%s\n' "$out" | grep -q 'test is successful' || die 2 "nginx -t did not report success: $out"
  local ver; ver=$(docker run --rm --entrypoint nginx "$IMG_ID" -v 2>&1 | sed -E 's/.*nginx\/([0-9.]+).*/\1/')
  [ "$(printf '%s\n1.27.3\n' "$ver" | sort -V | head -1)" = "1.27.3" ] || die 2 "nginx $ver is older than 1.27.3 (needed for resolve in upstream)"
  # 3. the running nginx must pass the same smoke list now, or a failure afterwards could not be judged
  local bad
  bad=$(smoke_once) || die 2 "the CURRENT nginx fails the smoke list before any change:$bad"
  log "preflight OK: $PRE -> $CAND, nginx $ver, image ${IMG_ID:0:19} (${IMG_REF}), effective model changes only the nginx service, current smoke OK"
  phase preflight
}

# ── return to the pre-S2 nginx, limited to the S2 commit ──────────────────────────────────
old_source() {       # $1 path: the pre-S2 content, from the saved copy or from git
  if [ -f "$STATE_DIR/bundle/$1" ]; then cat "$STATE_DIR/bundle/$1"; else git show "$CAND^:$1"; fi
}

# the files of the old mounts must exist and be the old ones: Docker would create a DIRECTORY at a missing source
restore_old_files() {
  mk_tmp; mkdir -p "$STATE_DIR/overwritten/nginx"
  local f
  for f in "${OLD_FILES[@]}"; do
    old_source "$f" > "$TMP/old.file" || { log "cannot read the pre-S2 $f"; return 1; }
    if [ ! -f "$f" ] || ! diff <(tr -d '\r' < "$f") <(tr -d '\r' < "$TMP/old.file") > /dev/null; then
      [ -f "$f" ] && cp -p "$f" "$STATE_DIR/overwritten/$f"
      mkdir -p nginx; cat "$TMP/old.file" > "$f"
      log "restored $f (a mount source of the previous definition) from the pre-S2 version"
    fi
  done
}

# The nginx service block of a compose file: from its `  nginx:` line to the next line that is not indented by four spaces
# or more. Blank lines inside the block belong to it when more of the block follows; trailing ones do not.
extract_block() {
  awk '/^  nginx:[[:space:]]*$/ && !done { f = 1; done = 1; print; next }
       f { if ($0 ~ /^[[:space:]]*$/) { pend = pend $0 "\n"; next }
           if ($0 ~ /^    /) { printf "%s", pend; pend = ""; print; next }
           exit }' "$1"
}

# without git history: replace the nginx service block of docker-compose.yml and nothing else in that file.
# Nothing is written unless the EFFECTIVE model of the new file has exactly the pre-S2 nginx hash, every other service keeps the
# hash it has now, and Compose succeeded for all three files. The original file is replaced only after all of that.
splice_compose() {
  mk_tmp; mkdir -p "$STATE_DIR/overwritten"
  old_source docker-compose.yml > "$TMP/pre.compose" || { log "cannot read the pre-S2 docker-compose.yml; nothing was changed"; return 1; }
  extract_block "$TMP/pre.compose" > "$TMP/pre.block"
  [ -s "$TMP/pre.block" ] || { log "no nginx service block in the pre-S2 docker-compose.yml; nothing was changed"; return 1; }
  if grep -q $'\r' docker-compose.yml; then sed -i 's/$/\r/' "$TMP/pre.block"; fi
  awk -v blockfile="$TMP/pre.block" 'BEGIN { while ((getline l < blockfile) > 0) blk = blk l "\n" }
       st == 0 && /^  nginx:[[:space:]]*$/ { printf "%s", blk; st = 1; next }
       st == 1 { if ($0 ~ /^[[:space:]]*$/) { pend = pend $0 "\n"; next }
                 if ($0 ~ /^    /) { pend = ""; next }
                 st = 2; printf "%s", pend; pend = ""; print; next }
       { print }
       END { if (st == 1) printf "%s", pend }' docker-compose.yml > "$TMP/new.compose"
  eff_hash_file "$TMP/pre.compose" "$TMP/h.pre" && eff_hash_file docker-compose.yml "$TMP/h.cur" && eff_hash_file "$TMP/new.compose" "$TMP/h.new" \
    || { log "docker compose config failed for the pre-S2, the current or the spliced file; nothing was changed"; return 1; }
  local old_h new_h
  old_h=$(svc_hash "$TMP/h.pre" nginx) && new_h=$(svc_hash "$TMP/h.new" nginx) \
    || { log "no single non-empty nginx hash in the pre-S2 or the spliced model; nothing was changed"; return 1; }
  [ "$old_h" = "$new_h" ] || { log "the nginx service of the spliced file differs from the pre-S2 one; nothing was changed"; return 1; }
  [ "$(grep -v '^nginx ' "$TMP/h.cur")" = "$(grep -v '^nginx ' "$TMP/h.new")" ] \
    || { log "the splice would change a service other than nginx; nothing was changed"; return 1; }
  if ! cmp -s docker-compose.yml "$TMP/new.compose"; then
    cp -p docker-compose.yml "$STATE_DIR/overwritten/docker-compose.yml"
    cat "$TMP/new.compose" > docker-compose.yml
    log "docker-compose.yml: only the nginx service block replaced (previous file saved in $STATE_DIR/overwritten); the tree is now modified relative to HEAD"
  fi
}

# The revert is made in a SCRATCH worktree and only a finished commit is fast-forwarded into this checkout:
# a conflict never exists in the real tree, nothing needs `revert --abort` (which resets), no reset or
# checkout of paths is ever run here. Only the scratch worktree this function created is removed; other
# worktree metadata is not touched. Returns 1, checkout untouched, on a conflict or when local changes
# stop the fast-forward.
revert_commit() {
  mk_tmp
  local wt="$TMP/revert-wt" out new
  git worktree add -q --detach "$wt" HEAD >> "$LOG" 2>&1 || { log "cannot create the scratch worktree"; return 1; }
  if out=$(git -C "$wt" -c user.name=s2-rollback -c user.email=s2-rollback@localhost -c commit.gpgsign=false revert --no-edit "$CAND" 2>&1); then
    new=$(git -C "$wt" rev-parse HEAD)
    git worktree remove --force "$wt" >> "$LOG" 2>&1 || log "the scratch worktree $wt could not be removed; remove it with: git worktree remove --force $wt"
    if out=$(git merge --ff-only "$new" 2>&1); then return 0; fi
    log "the revert commit could not be fast-forwarded into this checkout (local changes in files it touches): $(printf '%s' "$out" | head -3 | tr '\n' ' ')"
    return 1
  fi
  log "git revert not possible: $(printf '%s' "$out" | head -3 | tr '\n' ' ')"
  log "conflicting files: $(git -C "$wt" diff --name-only --diff-filter=U | tr '\n' ' ')"
  git worktree remove --force "$wt" >> "$LOG" 2>&1 || log "the scratch worktree $wt could not be removed; remove it with: git worktree remove --force $wt"
  return 1
}

# The Compose project of the model that `up` uses: the top-level `name:` of the effective model (nothing else of it is kept)
expected_project() { compose config 2>/dev/null | sed -n 's/^name: *//p' | head -1 | tr -d "\"'"; }

# Does this container belong to the expected Compose project AND to its nginx service? Only the labels decide, never the name.
container_belongs() {   # $1 = container ID or name, $2 = project
  local lbl
  lbl=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}' "$1" 2>/dev/null) || return 1
  [ -n "$2" ] && [ "$lbl" = "$2|nginx" ]
}

# Removes the nginx containers of THIS project that a killed `up` left behind (half-made, renamed, stopped). A container is removed only after
# its labels show project = the expected project and service = nginx; that includes a container found only because it holds the name. Anything else
# that holds the name is a NAME COLLISION: it is reported with its labels and kept untouched, and the return fails (returns 1).
clear_nginx_containers() {
  local project ids id holder shown
  project=$(expected_project)
  [ -n "$project" ] || { log "cannot determine the Compose project of the model; no container is removed"; return 1; }
  ids=$(compose ps -a -q nginx 2>/dev/null)
  for id in $ids; do
    container_belongs "$id" "$project" || { log "NAME COLLISION: container ${id:0:12} is listed for the nginx service but its labels are not project '$project' / service nginx; it was NOT removed"; return 1; }
    log "removing container ${id:0:12} (project $project, service nginx)"
    docker rm -f "$id" >> "$LOG" 2>&1 || true
  done
  holder=$(docker inspect --format '{{.Id}}' "$CONTAINER" 2>/dev/null) || return 0       # nobody holds the name
  if container_belongs "$holder" "$project"; then
    log "removing container ${holder:0:12} that holds the name $CONTAINER (project $project, service nginx)"
    docker rm -f "$holder" >> "$LOG" 2>&1 || true
    return 0
  fi
  shown=$(docker inspect --format 'project={{index .Config.Labels "com.docker.compose.project"}} service={{index .Config.Labels "com.docker.compose.service"}} image={{.Config.Image}}' "$holder" 2>/dev/null)
  log "NAME COLLISION: container $CONTAINER (${holder:0:12}, ${shown:-labels unreadable}) holds the name of the nginx container but does not belong to project '$project' / service nginx; it was NOT removed. Remove or rename it yourself, then run: $0 rollback $CAND"
  return 1
}

# The return recreates nginx from the image captured BEFORE the switch (by ID), whatever happened to the tag since. The evidence of
# a deploy of THIS candidate is used; without it the image of the container that runs now (S2 changes no image) is captured.
load_return_pin() {
  local orig=""
  if [ -f "$IMAGES_ENV" ] && [ "$(evidence_get CANDIDATE)" = "$CAND" ]; then
    orig=$(evidence_get ORIGINAL_IMAGE_ID)
    log "return image from the saved evidence ($IMAGES_ENV): $orig"
  else
    orig=$(docker inspect --format '{{.Image}}' "$CONTAINER" 2>/dev/null || true)
    if [ -n "$orig" ]; then
      log "no evidence of a deploy of this candidate in $STATE_DIR; the return uses the image of the running container: $orig"
    else
      log "WARNING: no evidence and no container: the return falls back to the image TAG of the compose model"
      return 0
    fi
  fi
  docker image inspect "$orig" > /dev/null 2>&1 || { log "the original image $orig is not present locally (the return never pulls)"; return 1; }
  write_pin "$orig" || { log "cannot write the image pin"; return 1; }
}

rollback_core() {
  trap '' HUP INT TERM
  log "returning nginx to the pre-S2 definition (only the S2 commit is undone)"
  phase returning
  local mode=""
  if git cat-file -e "HEAD:nginx/conf/nginx.conf" 2>/dev/null; then
    if revert_commit; then
      mode=revert; log "git revert $CAND done: one new local commit, every other commit and file untouched"
    else
      splice_compose || return 1
      mode=restore
    fi
  else
    mode=already-absent; log "S2 files are not in HEAD; only the container is (re)created"
  fi
  restore_old_files || return 1
  load_return_pin || return 1
  # An earlier `up` that was killed half-way can leave a renamed, stopped or half-made nginx container (Compose 5.3.1 then fails with
  # "container name already in use"; another version leaves a stopped container that a plain `up` does not repair). The nginx service is
  # stateless: after a failed or ineffective first try its containers are removed and the second try starts from a clean slate.
  local try
  for try in 1 2; do
    if up_logged "$RETURN_UP_DEADLINE" up -d --no-deps --pull never nginx; then
      case "$(cstate)" in running\|*|restarting\|*) break ;; esac
      log "nginx is not running after up ($(cstate || echo missing))"
    else
      log "docker compose up failed during the return"
    fi
    [ "$try" = 2 ] && { log "the return could not create a running nginx"; return 1; }
    log "removing the nginx containers of the project (by labels) and trying once more"
    clear_nginx_containers || return 1
  done
  if [ -n "$PIN_ID" ] && [ "$(docker inspect --format '{{.Image}}' "$CONTAINER" 2>/dev/null)" != "$PIN_ID" ]; then
    log "the returned container does not run the captured original image $PIN_ID"; return 1
  fi
  wait_ready 0 || return 1
  smoke || return 1
  log "S2_RESULT status=returned mode=$mode"
  return 0
}

fail() {
  log "FAIL: $1"
  if [ "$AUTO_ROLLBACK" = 1 ]; then
    if rollback_core; then exit 4; fi
    log "THE RETURN FAILED: ingress may be down. Fix the cause shown above (for example a missing upstream container), then run: $0 rollback $CAND"
    exit 5
  fi
  log "left as is (S2_AUTO_ROLLBACK=0); to return run: $0 rollback $CAND"
  exit 3
}

deploy() {
  preflight "$1"
  mkdir -p "$STATE_DIR/bundle/nginx"
  local f
  for f in docker-compose.yml "${OLD_FILES[@]}"; do git show "$BASE:$f" > "$STATE_DIR/bundle/$f" || die 2 "cannot save $f"; done
  ( cd "$STATE_DIR/bundle" && sha256sum docker-compose.yml "${OLD_FILES[@]}" > ../bundle.sha256 )
  project_ids > "$STATE_DIR/ids.before"
  save_image_evidence
  write_pin "$IMG_ID" || die 2 "cannot write the image pin"
  trap '' HUP INT TERM        # from here an SSH drop or Ctrl-C must not stop the switch half-way (before this point nothing was changed)
  if [ "$PRE" != "$CAND" ]; then
    git merge --ff-only "$CAND" >> "$LOG" 2>&1 || die 3 "fast-forward failed; the checkout was not changed"
  fi
  phase merged
  git diff --quiet "$CAND" -- docker-compose.yml nginx/conf && diff -r "$TMP/cand/nginx/conf" nginx/conf > /dev/null 2>&1 \
    || fail "the checkout differs from the validated candidate"
  # what up will read now must be the model that was validated, and the image tag must still be the validated image
  local now_hash after_out
  after_out=$(compose config --hash '*' 2>/dev/null) && [ -n "$after_out" ] || fail "docker compose config fails after the merge"
  printf '%s\n' "$after_out" | sort > "$TMP/after.hash"
  now_hash=$(svc_hash "$TMP/after.hash" nginx) || fail "no single non-empty nginx hash after the merge"
  [ "$now_hash" = "$CAND_NGINX_HASH" ] || fail "the effective nginx model after the merge differs from the validated one"
  [ "$(docker image inspect --format '{{.Id}}' "$IMG_REF" 2>/dev/null)" = "$IMG_ID" ] || fail "image $IMG_REF no longer is the validated image $IMG_ID (tag moved)"
  local t0 t1 t2 t3 dl; t0=$(now)
  log "recreating $CONTAINER (compose up --no-deps --pull never nginx on image ID $IMG_ID, deadline ${UP_DEADLINE}s)"
  up_logged "$UP_DEADLINE" up -d --no-deps --pull never nginx || fail "docker compose up failed or exceeded ${UP_DEADLINE}s"
  t1=$(now)
  phase recreated
  [ "$(docker inspect --format '{{.Image}}' "$CONTAINER" 2>/dev/null)" = "$IMG_ID" ] || fail "the new container does not run the validated image $IMG_ID"
  dl=$(( $(date +%s) + 30 ))       # only to time the first answer; health and smoke below have the real deadlines
  until [ "$(http_status 80 /health)" = 200 ]; do
    [ "$(date +%s)" -ge "$dl" ] && break
    sleep 0.2
  done
  t2=$(now)
  wait_ready 1 || fail "the recreated nginx is not healthy"
  smoke || fail "smoke failed on the recreated nginx"
  project_ids | grep -v "^/$CONTAINER " > "$STATE_DIR/ids.after.others"
  grep -v "^/$CONTAINER " "$STATE_DIR/ids.before" | diff - "$STATE_DIR/ids.after.others" > /dev/null || fail "another container of the project changed"
  t3=$(now)
  log "S2_RESULT status=ok up_seconds=$(secs "$t0" "$t1") first_answer_seconds=$(secs "$t0" "$t2") healthy_and_smoke_seconds=$(secs "$t0" "$t3") from=$PRE to=$CAND"
}

main() {
  local cmd=${1:-} sha=${2:-}
  case "$cmd" in
    smoke) load_compose_args; smoke && exit 0; exit 2 ;;
    preflight|deploy|rollback) [ -n "$sha" ] || { sed -n '2,12p' "$0"; exit 2; } ;;
    *) sed -n '2,12p' "$0"; exit 2 ;;
  esac
  load_compose_args
  check_default_mode
  echo "$$" > "$STATE_DIR/pid"
  case "$cmd" in
    preflight) preflight "$sha"; log "preflight only: nothing was changed" ;;
    deploy)    deploy "$sha" ;;
    rollback)
      CAND=$(git rev-parse --verify --quiet "$sha^{commit}") || die 5 "$sha is not a commit here"
      if rollback_core; then exit 0; fi
      log "THE RETURN FAILED: ingress may be down. Fix the cause shown above, then run it again: $0 rollback $CAND"
      exit 5 ;;
  esac
}

if [ -z "${S2_SOURCE_ONLY:-}" ]; then main "$@"; fi
