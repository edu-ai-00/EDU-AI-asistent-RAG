#!/usr/bin/env bash
# R2 (the trust boundary of CF-Connecting-IP): roll the nginx / cloudflared wiring forward in three steps and back, with checks.
#
#   nginx_r2_switch.sh smoke                     the smoke list against the running nginx (read-only)
#   nginx_r2_switch.sh status                    the step the checkout is at and the observed wiring (read-only)
#   nginx_r2_switch.sh preflight <step> <SHA>    validates the step commit, changes nothing
#   nginx_r2_switch.sh apply     <step> <SHA>    preflight, fast-forward, the step, checks; the step is returned automatically if a check fails
#   nginx_r2_switch.sh rollback  <step> <SHA>    return ONE step (the highest applied one) by reverting its commit and putting the containers back;
#                                                the step (1-3) and the commit are judged first, a wrong pair is refused with 2 and changes nothing
#   nginx_r2_switch.sh rollback-all              return every step recorded in the state directory, highest first (records judged first)
#
# A step commit is accepted only as the ONE commit that changes exactly the files of its step, as regular-file modifications, and moves the
# deployed files from the previous step to this one; the fast-forward may bring nothing else than docs, these tools and their tests
# ($ALLOWED_EXTRA); and for step 2 the effective Compose model must differ from the current one by exactly the designed change
# (`tunnel_subnet.py model-delta`): a port, a mount, a command, a capability or any other field is refused.
#
#   step 1  nginx/conf: the trust file is included, X-Forwarded-For = $remote_addr, the OLD range stays trusted. Reload only.
#   step 2  docker-compose.yml: networks `tunnel` / `tunnel_egress`, the trust config; nginx, then cloudflared, are recreated on the
#           SAME image IDs. nginx trusts the new subnet AND the old range; cloudflared moves from `edge` to `tunnel` + `tunnel_egress`.
#   step 3  nginx/conf: the old range is removed. Reload only. Nothing but the tunnel network is trusted from now on.
#
# Only the services nginx and cloudflared are ever recreated; no other service, no data and no .env is touched. The effective Compose
# model of every other service must stay identical (checked by `docker compose config --hash`). The subnet (EDU_AI_TUNNEL_SUBNET) is a
# setting of the installation that must already be in the environment of Compose (step 0, done by the owner after `tunnel_subnet.py
# check` reported 0); this script never writes it and never reads .env for its value.
#
# Run it from the deployment checkout (cwd), from a COPY taken out of the fetched commit, never from the tree it is about to change:
#     git show <SHA>:scripts/nginx_r2_switch.sh > ~/r2-switch.sh; git show <SHA>:scripts/tunnel_subnet.py > ~/tunnel_subnet.py
# It never runs reset, clean, stash, path checkout or a force push. It is NOT the S2 script and does not replace it: while any step is
# applied the S2 return goes through scripts/nginx_s2_guard.sh, which refuses until `rollback-all` has returned R2 (the S2 script is unchanged).
#
# The Compose model is the EFFECTIVE one: validation and `up` use the same arguments, environment and override files, only the base
# file is replaced by the candidate's for validation. `R2_COMPOSE_ARGS_FILE` holds one `docker compose` argument per line and the line
# `@BASE@` marks the base file. Without it .env (or the environment) must pin COMPOSE_FILE=docker-compose.yml.
#
# The containers are created from IMAGE IDs (the ones captured before the first step), never from tags; the IDs and every other fact
# needed to return are kept in $R2_STATE_DIR (keep it until the return window ends) and the images by private tags $R2_EVIDENCE_REPO/...
#
# Probes are short-lived containers of the nginx image (labelled r2.probe=1, --rm). One on `tunnel` stands in for cloudflared and one on
# `edge` for any other member of that network; they send a forged CF-Connecting-IP to a path nginx answers itself and the access log
# says whose address nginx used. A probe on `tunnel` is, for its few seconds, a member of the trusted network: that is the point of it.
# They are NOT a proof of real Cloudflare traffic. The routes of the tunnel (hostname -> service) are read from cloudflared's own log
# (hostname and service only) before and after, and every service target is reached from the network that cloudflared uses for it.
#
# Exit status: 0 ok | 2 refused before any change | 3 failed, left as is (R2_AUTO_ROLLBACK=0)
#              4 failed and returned | 5 the return itself failed | 99 test hook
#
# Environment (all optional):
#   R2_NGINX_CONTAINER=edu_ai_nginx  R2_CLOUDFLARED_CONTAINER=cloudflared  R2_STATE_DIR=$HOME/r2-state
#   R2_SUBNET_TOOL=<dir of this script>/tunnel_subnet.py  R2_PYTHON=python3
#   R2_ROUTES_MODE=host|docker (how the preflight reads routes; host on a Linux host)  R2_HELPER_IMAGE=<image for docker mode>
#   R2_RESERVED="CIDR CIDR"  ranges the host does not know (LAN, VPN, VPC) that the subnet must not overlap
#   R2_HEALTH_DEADLINE=90 R2_SMOKE_DEADLINE=30 R2_UP_DEADLINE=120 R2_RETURN_UP_DEADLINE=120 R2_RELOAD_DEADLINE=30 R2_INGRESS_DEADLINE=90
#   R2_COMPOSE_ARGS_FILE=FILE  R2_SMOKE_LIST="port|path|status[,status]" lines  R2_AUTO_ROLLBACK=1  R2_EVIDENCE_REPO=r2-evidence
#   R2_PROBE_IMAGE=<image ID>  R2_PROBE_NAME=edu_ai_nginx  R2_INGRESS_REQUIRED=1 (refuse step 2 when no route can be read from the log)
#   test hooks: R2_TEST_STOP_AFTER=<phase> (exit 99)  R2_TEST_FAIL_AT=<phase> (inject a failure)  R2_TEST_SLEEP_AFTER=<phase>:<seconds>
#               phases: preflight merged reloaded nginx_recreated cloudflared_recreated verified      R2_SOURCE_ONLY=1 (load the functions)

set -u
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

NGINX=${R2_NGINX_CONTAINER:-edu_ai_nginx}
CF=${R2_CLOUDFLARED_CONTAINER:-cloudflared}
STATE_DIR=${R2_STATE_DIR:-$HOME/r2-state}
HEALTH_DEADLINE=${R2_HEALTH_DEADLINE:-90}
SMOKE_DEADLINE=${R2_SMOKE_DEADLINE:-30}
UP_DEADLINE=${R2_UP_DEADLINE:-120}
RETURN_UP_DEADLINE=${R2_RETURN_UP_DEADLINE:-120}
RELOAD_DEADLINE=${R2_RELOAD_DEADLINE:-30}
INGRESS_DEADLINE=${R2_INGRESS_DEADLINE:-90}
AUTO_ROLLBACK=${R2_AUTO_ROLLBACK:-1}
PY=${R2_PYTHON:-python3}
TOOL=${R2_SUBNET_TOOL:-$(dirname "${BASH_SOURCE[0]}")/tunnel_subnet.py}
ROUTES_MODE=${R2_ROUTES_MODE:-host}
HELPER_IMAGE=${R2_HELPER_IMAGE:-}
RESERVED=${R2_RESERVED:-}
EVIDENCE_REPO=${R2_EVIDENCE_REPO:-r2-evidence}
PROBE_NAME=${R2_PROBE_NAME:-edu_ai_nginx}
INGRESS_REQUIRED=${R2_INGRESS_REQUIRED:-1}
FORGED_IP=203.0.113.77                                   # TEST-NET-3: nothing real has it
LEGACY_RANGE=172.16.0.0/12
BASE_MARK='@BASE@'
COMPOSE_ARGS=()
# the files each step changes: its commit changes exactly these and nothing else
step_files() { case $1 in 1) echo "nginx/conf/nginx.conf nginx/conf/search_api.conf" ;; 2) echo "docker-compose.yml" ;; 3) echo "nginx/conf/nginx.conf" ;; esac; }
step_set()   { step_files "$1" | tr ' ' '\n' | sort | tr '\n' ' '; }
# what a fast-forward to a step may bring besides the step's own files: documentation, these tools and their tests
ALLOWED_EXTRA='docs/* tests/README.md tests/nginx_r2/* scripts/nginx_r2_switch.sh scripts/tunnel_subnet.py scripts/nginx_s2_guard.sh'
# Every request is answered from inside the nginx container; statuses come from nginx itself or from the upstreams' health endpoints
# (the list that S2 read from production, statuses only).
SMOKE_LIST=${R2_SMOKE_LIST:-'80|/health|200
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
STEP=""; CAND=""; PRE=""; PROJECT=""; SUBNET=""
ORIG_NGINX_IMG=""; ORIG_CF_IMG=""; PIN_ON=0
FORWARD=0
trap '[ -z "$TMP" ] || rm -rf "$TMP"' EXIT

log()  { printf '%s %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$LOG"; }
die()  { local code=$1; shift; log "FAIL: $*"; exit "$code"; }
now()  { date +%s.%N; }
secs() { awk -v a="$1" -v b="$2" 'BEGIN { printf "%.1f", b - a }'; }
abs()  { ( cd "$1" && { pwd -W 2>/dev/null || pwd; } ); }
mk_tmp() { [ -n "$TMP" ] || TMP=$(mktemp -d); }

# ── how Compose is called: ONE definition for validation and for `up` ─────────────────────────
load_compose_args() {
  COMPOSE_ARGS=()
  [ -n "${R2_COMPOSE_ARGS_FILE:-}" ] || return 0
  [ -r "$R2_COMPOSE_ARGS_FILE" ] || die 2 "R2_COMPOSE_ARGS_FILE '$R2_COMPOSE_ARGS_FILE' is not readable: refusing to fall back to the defaults"
  mapfile -t COMPOSE_ARGS < "$R2_COMPOSE_ARGS_FILE"
  COMPOSE_ARGS=("${COMPOSE_ARGS[@]%$'\r'}")
  [ "${#COMPOSE_ARGS[@]}" -gt 0 ] || die 2 "R2_COMPOSE_ARGS_FILE '$R2_COMPOSE_ARGS_FILE' is empty"
  local n=0 x
  for x in "${COMPOSE_ARGS[@]}"; do [ "$x" = "$BASE_MARK" ] && n=$((n + 1)); done
  [ "$n" = 1 ] || die 2 "R2_COMPOSE_ARGS_FILE must contain the line $BASE_MARK exactly once (it marks the base compose file), found $n"
}

check_default_mode() {
  [ "${#COMPOSE_ARGS[@]}" -eq 0 ] || return 0
  local cf=${COMPOSE_FILE:-}
  if [ -z "$cf" ] && [ -f .env ]; then
    cf=$(grep -E '^COMPOSE_FILE=' .env | tail -1 | cut -d= -f2- | tr -d "\"' " | tr -d '\r')
  fi
  [ "$cf" = docker-compose.yml ] || die 2 "without R2_COMPOSE_ARGS_FILE the environment or .env must pin COMPOSE_FILE=docker-compose.yml (found '${cf:-nothing}'); otherwise the files validated here are not the files up reads"
}

build_args() {          # $1 = what stands for the base file: its path, or - for stdin; result in A
  A=(); local x
  for x in "${COMPOSE_ARGS[@]+"${COMPOSE_ARGS[@]}"}"; do
    if [ "$x" = "$BASE_MARK" ]; then A+=("$1"); else A+=("$x"); fi
  done
}
base_path() { printf '%s/docker-compose.yml' "$(abs .)"; }
compose()   { build_args "$(base_path)"; docker compose "${A[@]+"${A[@]}"}" "$@"; }
compose_with_base() {   # the same arguments, environment and override files, but THIS file is the base (read from stdin)
  local f=$1; shift
  if [ "${#COMPOSE_ARGS[@]}" -gt 0 ]; then build_args -; docker compose "${A[@]}" "$@" < "$f"; else docker compose -f - "$@" < "$f"; fi
}

# ── images are referenced by ID, never by a tag, whenever this script creates nginx or cloudflared ──
pin_path()  { printf '%s/image-pin.yaml' "$(abs "$STATE_DIR")"; }
write_pin() {
  printf 'services:\n  nginx:\n    image: %s\n  cloudflared:\n    image: %s\n' "$1" "$2" > "$STATE_DIR/image-pin.yaml" || return 1
  PIN_ON=1
}
compose_up() {          # $1 = deadline in seconds, the rest are the `up` arguments; runs on the pinned image IDs
  local d=$1; shift
  build_args "$(base_path)"
  if [ "$PIN_ON" = 1 ]; then
    if [ "${#COMPOSE_ARGS[@]}" -eq 0 ]; then A=(-f "$(base_path)"); fi
    A+=(-f "$(pin_path)")
  fi
  timeout "$d" docker compose "${A[@]+"${A[@]}"}" "$@"
}
up_logged() {
  local out rc
  out=$(compose_up "$@" 2>&1); rc=$?
  printf '%s\n' "$out" >> "$LOG"
  [ "$rc" = 0 ] || log "docker compose said (rc=$rc): $(printf '%s' "$out" | tail -4 | tr '\n' ' ' | cut -c1-500)"
  return "$rc"
}

# ── evidence a LATER process needs to return without help ────────────────────────────────────
IMAGES_ENV="$STATE_DIR/images.env"
evidence_get() { sed -n "s/^$1=//p" "$IMAGES_ENV" 2>/dev/null | head -1; }
save_image_evidence() {
  local n c
  n=$(docker inspect --format '{{.Image}}' "$NGINX" 2>/dev/null) && c=$(docker inspect --format '{{.Image}}' "$CF" 2>/dev/null) || die 2 "cannot read the images of $NGINX and $CF"
  if [ -n "$(evidence_get NGINX_IMAGE_ID)" ]; then       # a later step: R2 changes no image, the running ones must be the captured ones
    [ "$(evidence_get NGINX_IMAGE_ID)" = "$n" ] && [ "$(evidence_get CLOUDFLARED_IMAGE_ID)" = "$c" ] \
      || die 2 "the running images differ from the ones captured at the first step ($IMAGES_ENV): R2 changes no image, someone else did"
  else
    docker image inspect "$n" > /dev/null 2>&1 && docker image inspect "$c" > /dev/null 2>&1 || die 2 "an original image is not present locally"
    # private tags keep both images alive (an untagged image can be pruned) whatever happens to the public tags
    docker tag "$n" "$EVIDENCE_REPO/nginx:original-${n#sha256:}" 2>/dev/null && docker tag "$c" "$EVIDENCE_REPO/cloudflared:original-${c#sha256:}" 2>/dev/null \
      || die 2 "cannot tag the original images"
    printf 'NGINX_IMAGE_ID=%s\nCLOUDFLARED_IMAGE_ID=%s\nPROJECT=%s\n' "$n" "$c" "$PROJECT" > "$IMAGES_ENV.new" && mv "$IMAGES_ENV.new" "$IMAGES_ENV" || die 2 "cannot write $IMAGES_ENV"
    log "image evidence saved in $IMAGES_ENV: nginx $n, cloudflared $c"
  fi
  ORIG_NGINX_IMG=$n; ORIG_CF_IMG=$c
}
load_evidence() {       # for a return in a new process
  ORIG_NGINX_IMG=$(evidence_get NGINX_IMAGE_ID); ORIG_CF_IMG=$(evidence_get CLOUDFLARED_IMAGE_ID)
  if [ -z "$ORIG_NGINX_IMG" ] || [ -z "$ORIG_CF_IMG" ]; then
    ORIG_NGINX_IMG=$(docker inspect --format '{{.Image}}' "$NGINX" 2>/dev/null || true)
    ORIG_CF_IMG=$(docker inspect --format '{{.Image}}' "$CF" 2>/dev/null || true)
    [ -n "$ORIG_NGINX_IMG" ] && [ -n "$ORIG_CF_IMG" ] || { log "no evidence and no containers: the images to return to are unknown"; return 1; }
    log "no evidence in $STATE_DIR: the return uses the images of the containers that run now (R2 changes no image)"
  fi
  docker image inspect "$ORIG_NGINX_IMG" > /dev/null 2>&1 && docker image inspect "$ORIG_CF_IMG" > /dev/null 2>&1 || { log "an original image is not present locally (the return never pulls)"; return 1; }
  write_pin "$ORIG_NGINX_IMG" "$ORIG_CF_IMG" || { log "cannot write the image pin"; return 1; }
}

phase() {
  echo "$1" > "$STATE_DIR/phase"; log "phase: $1"
  local h hooks=()
  IFS=, read -ra hooks <<< "${R2_TEST_SLEEP_AFTER:-}"
  for h in "${hooks[@]+"${hooks[@]}"}"; do [ "${h%%:*}" = "$1" ] && sleep "${h#*:}"; done
  if [ "${R2_TEST_STOP_AFTER:-}" = "$1" ]; then log "TEST HOOK: stopping after $1"; exit 99; fi
  if [ "$FORWARD" = 1 ] && [ "${R2_TEST_FAIL_AT:-}" = "$1" ]; then fail "injected failure at phase $1 (test hook)"; fi
  return 0
}

# ── observation: a failed probe is ERR, never a status ───────────────────────────────────────
cstate() { docker inspect --format '{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$1" 2>/dev/null; }
cfield() { docker inspect --format "$2" "$1" 2>/dev/null; }
cnets()  { docker inspect --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' "$1" 2>/dev/null | sed "s/^${PROJECT}_//" | sed '/^$/d' | sort | tr '\n' ' ' | sed 's/ $//'; }
http_status() {      # port path -> status of the FIRST response (a redirect is reported), or ERR
  local out st
  out=$(docker exec "$NGINX" wget -S -O /dev/null -T 5 "http://127.0.0.1:$1$2" 2>&1) || true
  st=$(printf '%s\n' "$out" | grep -E 'HTTP/[0-9.]+ [0-9]{3}' | head -1 | sed -E 's/.*HTTP\/[0-9.]+ ([0-9]{3}).*/\1/')
  if [ -n "$st" ]; then echo "$st"; else echo ERR; fi
}
wait_ready() {       # $1 = 1: a NEW container (healthcheck must exist and be healthy, no restart); 0: running and /health 200
  local deadline=$(( $(date +%s) + HEALTH_DEADLINE )) st n
  while :; do
    if ! st=$(cstate "$NGINX"); then log "container $NGINX does not exist"; return 1; fi
    if [ "$1" = 1 ]; then
      n=$(cfield "$NGINX" '{{.RestartCount}}' || echo 0)
      [ "${n:-0}" -gt 0 ] && { log "the new container was restarted ${n}x (crash loop), state $st"; return 1; }
    fi
    case "$st" in
      "running|healthy") return 0 ;;
      "running|unhealthy") log "container is unhealthy"; return 1 ;;
      "running|starting") ;;
      "running|none")
        if [ "$1" = 1 ]; then log "container has no healthcheck"; return 1; fi
        [ "$(http_status 80 /health)" = 200 ] && return 0 ;;
      restarting\|*) [ "$1" = 1 ] && { log "container state $st"; return 1; } ;;
      *) log "container state $st"; return 1 ;;
    esac
    if [ "$(date +%s)" -ge "$deadline" ]; then log "not ready within ${HEALTH_DEADLINE}s (state $st)"; return 1; fi
    sleep 1
  done
}
smoke_once() {
  local bad="" port path want got
  while IFS='|' read -r port path want; do
    [ -z "$port" ] && continue
    got=$(http_status "$port" "$path")
    case ",$want," in *",$got,"*) ;; *) bad="$bad $port$path(want $want, got $got)" ;; esac
  done <<< "$SMOKE_LIST"
  [ -z "$bad" ] && return 0
  printf '%s' "$bad"; return 1
}
smoke() {
  local deadline=$(( $(date +%s) + SMOKE_DEADLINE )) bad n
  n=$(printf '%s\n' "$SMOKE_LIST" | grep -c .)
  while :; do
    if bad=$(smoke_once); then log "smoke OK ($n requests)"; return 0; fi
    if [ "$(date +%s)" -ge "$deadline" ]; then log "smoke failed after ${SMOKE_DEADLINE}s:$bad"; return 1; fi
    sleep 1
  done
}
project_ids() { compose ps -a -q 2>/dev/null | xargs -r docker inspect --format '{{.Name}} {{.Id}}' 2>/dev/null | sort; }
others_unchanged() {    # every container of the project except nginx and cloudflared keeps its ID
  project_ids | grep -v -e "^/$NGINX " -e "^/$CF " > "$STATE_DIR/ids.now"
  diff "$STATE_DIR/ids.before" "$STATE_DIR/ids.now" > /dev/null
}
expected_project() { compose config 2>/dev/null | sed -n 's/^name: *//p' | head -1 | tr -d "\"'"; }

# ── tool, model and conf helpers ─────────────────────────────────────────────────────────────
tool() { "$PY" "$TOOL" "$@"; }
model_cur()  { compose config --format json 2>/dev/null; }
model_cand() { compose_with_base "$1" config --format json 2>"$TMP/model.err"; }
model_pair() {          # both effective models as ONE JSON object for `tool model-delta`; the models never touch the disk
  printf '{"current":'; model_cur; printf ',"candidate":'; model_cand "$1"; printf '}'
}
changed_services() {    # services whose effective hash differs ($1 current, $2 candidate hash files)
  diff "$1" "$2" | sed -n 's/^> \([^ ]*\) .*/\1/p' | sort | tr '\n' ' '
}
hash_file() {           # $1 base file ('' = the checkout's own), $2 output; fails when Compose fails or prints nothing
  local out
  if [ -n "$1" ]; then out=$(compose_with_base "$1" config --hash '*' 2>"$TMP/model.err") || return 1; else out=$(compose config --hash '*' 2>"$TMP/model.err") || return 1; fi
  [ -n "$out" ] || return 1
  printf '%s\n' "$out" | sort > "$2"
}
conf_directives() { grep -v '^[[:space:]]*#' "${1:-nginx/conf/nginx.conf}"; }
step_from() {           # $1 nginx.conf text, $2 docker-compose.yml text: the step these files show (0..3)
  local d; d=$(printf '%s\n' "$1" | grep -v '^[[:space:]]*#')
  printf '%s\n' "$d" | grep -q 'include /etc/edu_ai/tunnel/\*\.conf;' || { echo 0; return; }
  printf '%s\n' "$2" | grep -v '^[[:space:]]*#' | grep -q 'tunnel_trust' || { echo 1; return; }
  if printf '%s\n' "$d" | grep -qE '^[[:space:]]*set_real_ip_from[[:space:]]+172\.16\.0\.0/12;'; then echo 2; else echo 3; fi
}
observed_step() {       # the step the CHECKOUT is at, from its files
  [ -f nginx/conf/nginx.conf ] || { echo 0; return; }
  step_from "$(cat nginx/conf/nginx.conf)" "$(cat docker-compose.yml 2>/dev/null)"
}
tree_step() { step_from "$(git show "$1:nginx/conf/nginx.conf" 2>/dev/null)" "$(git show "$1:docker-compose.yml" 2>/dev/null)"; }   # the step a commit shows

# ── what a step commit is, and which commit belongs to which step (pure reads: nothing is changed) ──
extra_allowed() { local pats p; IFS=' ' read -ra pats <<< "$ALLOWED_EXTRA"; for p in "${pats[@]}"; do case $1 in $p) return 0 ;; esac; done; return 1; }
outside_scope() {       # $1 from, $2 to, $3 step: the paths a fast-forward would change that are neither the step's files nor an allowed extra
  local f out=""
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    case " $(step_files "$3") " in *" $f "*) continue ;; esac
    extra_allowed "$f" || out="$out $f"
  done < <(git diff --name-only --no-renames "$1" "$2")
  printf '%s' "$out"
}
validate_step_commit() {   # $1 step, $2 commit: the ONE commit that moves the deployment from step $1-1 to $1 and touches nothing else
  local n=$1 c=$2 got bad
  [ "$(git rev-list --parents -n1 "$c" | wc -w)" -eq 2 ] || { log "the candidate is a merge commit; it must be the single commit of the step"; return 1; }
  got=$(git diff --name-only --no-renames "$c^" "$c" | sort | tr '\n' ' ')
  [ "$got" = "$(step_set "$n")" ] || { log "the candidate does not change exactly the files of step $n ($(step_files "$n")): $got"; return 1; }
  bad=$(git diff --raw --no-renames "$c^" "$c" | grep -vE '^:100644 100644 [^ ]+ [^ ]+ M[[:space:]]' | cut -f2 | tr '\n' ' ')
  [ -z "$bad" ] || { log "the candidate changes more than the content of regular files: $bad"; return 1; }
  [ "$(tree_step "$c^")" = "$((n - 1))" ] && [ "$(tree_step "$c")" = "$n" ] \
    || { log "the files of the candidate do not move the deployment from step $((n - 1)) to step $n"; return 1; }
}
recorded_commit()  { grep "^$1 " "$STATE_DIR/steps.log" 2>/dev/null | tail -1 | cut -d' ' -f2; }      # the commit recorded for step $1
recorded_step_of() { awk -v c="$1" '$2 == c { s = $1 } END { print s }' "$STATE_DIR/steps.log" 2>/dev/null; }   # the step recorded for commit $1
validate_return_commit() {   # $1 step, $2 commit as given; sets CAND. Without a record the commit is judged on its content alone.
  local n=$1 rec other
  case "$n" in 1|2|3) ;; *) log "the step must be 1, 2 or 3 (got '$n')"; return 1 ;; esac
  CAND=$(git rev-parse --verify --quiet "$2^{commit}") || { log "$2 is not a commit here"; return 1; }
  validate_step_commit "$n" "$CAND" || return 1
  rec=$(recorded_commit "$n"); other=$(recorded_step_of "$CAND")
  [ -z "$rec" ] || [ "$rec" = "$CAND" ] || { log "step $n was applied from ${rec:0:10} (record in $STATE_DIR), not from ${CAND:0:10}"; return 1; }
  [ -z "$other" ] || [ "$other" = "$n" ] || { log "${CAND:0:10} is recorded as step $other, not as step $n"; return 1; }
  [ -n "$rec" ] || log "no record of step $n in $STATE_DIR: ${CAND:0:10} is accepted on its content (the one commit that changes exactly the files of step $n and moves the deployment from step $((n - 1)) to $n)"
  return 0
}
validate_return_state() {    # $1 step; CAND is set: the checkout must allow returning that step now
  local n=$1 obs; obs=$(observed_step)
  [ "$obs" -le "$n" ] || { log "step $n cannot be returned while the checkout is at step $obs: return step $obs first"; return 1; }
  [ "$obs" -lt "$n" ] || git merge-base --is-ancestor "$CAND" HEAD || { log "the checkout is at step $n but ${CAND:0:10} is not in HEAD: it did not put step $n there"; return 1; }
}
nginx_workers() { docker exec "$NGINX" sh -c "ps -o pid,args | grep 'nginx: worker' | grep -v grep | awk '{print \$1}' | sort -n | tr '\n' ' '" 2>/dev/null; }

# ── the routes of the tunnel, from cloudflared's own log: hostname and service, nothing else of the line is kept ──
cf_ingress() {
  docker logs "$CF" 2>&1 | grep 'Updated to new configuration' | tail -1 \
    | grep -oE '\\"(hostname|service)\\":\\"[^\\]*\\"' | sed -E 's/\\"//g' \
    | awk '{ k = $0; sub(/:.*/, "", k); v = substr($0, length(k) + 2) }
           k == "hostname" { h = v; next }
           k == "service"  { print (h == "" ? "*" : h) "|" v; h = "" }' | sort
}
cf_registered() { docker logs "$CF" 2>&1 | grep -c 'Registered tunnel connection'; }
wait_ingress_same() {   # the NEW cloudflared must report exactly the routes it reported before, and have registered a connection
  local deadline=$(( $(date +%s) + INGRESS_DEADLINE )) now n
  while :; do
    now=$(cf_ingress); n=$(cf_registered)
    if [ "$now" = "$(cat "$STATE_DIR/ingress.before")" ] && [ "${n:-0}" -ge 1 ]; then log "tunnel routes unchanged ($(printf '%s\n' "$now" | grep -c .) routes), $n connection(s) registered"; return 0; fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
      log "tunnel routes after the step differ or no connection registered within ${INGRESS_DEADLINE}s (routes: $(printf '%s' "$now" | tr '\n' ' '), connections: ${n:-0})"; return 1
    fi
    sleep 2
  done
}

# ── probes ───────────────────────────────────────────────────────────────────────────────────
probe_image() { printf '%s' "${R2_PROBE_IMAGE:-$ORIG_NGINX_IMG}"; }
probe() {               # $1 = network (short name), $2 = sh -c script; prints the script's output
  timeout 40 docker run --rm --network "${PROJECT}_$1" --label r2.probe=1 --entrypoint sh "$(probe_image)" -c "$2" 2>&1
}
probe_client_addr() {   # $1 network, $2 forged address: whose address nginx logged for a request to port 81 that carries a forged CF-Connecting-IP
  local id out own got
  id="r2p$(date +%s)$RANDOM"
  out=$(probe "$1" "echo OWN=\$(hostname -i); wget -S -O /dev/null -T 5 --header 'CF-Connecting-IP: $2' 'http://$PROBE_NAME:81/qa/?r2probe=$id' 2>&1 | grep -c 'HTTP/' | sed 's/^/REPLIED=/'") || true
  own=$(printf '%s\n' "$out" | sed -n 's/^OWN=//p' | awk '{print $1}')
  sleep 1
  got=$(docker logs --since 2m "$NGINX" 2>&1 | grep "r2probe=$id" | head -1 | awk '{print $1}')
  [ -n "$own" ] && [ -n "$got" ] || { echo "ERR own=${own:-?} logged=${got:-none}"; return 1; }
  if [ "$got" = "$2" ]; then echo "HONOURED $got"; elif [ "$got" = "$own" ]; then echo "IGNORED $got"; else echo "OTHER own=$own logged=$got"; fi
}
want_probe() {          # $1 network $2 expected word (HONOURED|IGNORED) $3 what for
  local r; r=$(probe_client_addr "$1" "$FORGED_IP")
  case "$r" in "$2 "*) log "probe from $1: $r ($3)"; return 0 ;; esac
  log "probe from $1 gave '$r', expected $2 ($3)"; return 1
}
targets_reachable() {   # every service of the tunnel's routes is reached from the network cloudflared uses for it
  local line svc host port net rc=0 st
  while IFS= read -r line; do
    svc=${line#*|}
    case "$svc" in http://*|https://*) ;; *) continue ;; esac       # http_status:404 and friends have no target
    host=${svc#*://}; host=${host%%/*}; port=${host##*:}; host=${host%%:*}
    [ "$port" = "$host" ] && port=80
    if [ "$host" = "$PROBE_NAME" ] || [ "$host" = "$NGINX" ]; then
      if [ "$(observed_step)" -ge 2 ]; then net=tunnel; else net=edge; fi
    else
      net=app
    fi
    st=$(probe "$net" "wget -S -O /dev/null -T 5 'http://$host:$port/' 2>&1 | grep -E 'HTTP/[0-9.]+ [0-9]{3}' | head -1 | sed -E 's/.*HTTP\\/[0-9.]+ ([0-9]{3}).*/\\1/'") || true
    case "$st" in [0-9][0-9][0-9]) log "route target $svc is reached from $net (status $st)" ;; *) log "route target $svc is NOT reached from $net"; rc=1 ;; esac
  done < "$STATE_DIR/ingress.before"
  return $rc
}

# ── nginx -t on candidate files, in the image that runs, with the trust file when the step has one ─────────
nginx_t_candidate() {   # $1 conf dir, $2 trust file ('' = none)
  local out gflag=() mounts=()
  case "$(docker image inspect --format '{{json .Config.Cmd}}' "$ORIG_NGINX_IMG" 2>/dev/null)" in *'daemon off;'*) gflag=(-g 'daemon off;') ;; esac
  mounts=(--mount "type=bind,source=$(abs "$1"),target=/etc/nginx,readonly")
  [ -z "$2" ] || mounts+=(--mount "type=bind,source=$(abs "$(dirname "$2")")/$(basename "$2"),target=/etc/edu_ai/tunnel/trust.conf,readonly")
  out=$(docker run --rm --network none --entrypoint nginx "${mounts[@]}" "$ORIG_NGINX_IMG" -t "${gflag[@]+"${gflag[@]}"}" 2>&1) || { log "nginx -t failed: $out"; return 1; }
  printf '%s\n' "$out" | grep -q 'test is successful' || { log "nginx -t did not report success: $out"; return 1; }
}

# ── preflight: everything is checked BEFORE the checkout or a container is touched ─────────────
preflight() {
  STEP=$1
  case "$STEP" in 1|2|3) ;; *) die 2 "the step must be 1, 2 or 3" ;; esac
  CAND=$(git rev-parse --verify --quiet "$2^{commit}") || die 2 "candidate $2 is not a commit in this checkout (git fetch first)"
  PRE=$(git rev-parse HEAD)
  mk_tmp
  validate_step_commit "$STEP" "$CAND" || die 2 "the candidate is not the one commit of step $STEP"
  git merge-base --is-ancestor "$PRE" "$CAND" || die 2 "HEAD is not an ancestor of the candidate: no fast-forward"
  [ "$PRE" != "$CAND" ] || die 2 "HEAD already is the candidate: step $STEP is applied (use status)"
  [ -z "$(git diff --name-only "$PRE" "$CAND^" -- docker-compose.yml nginx)" ] \
    || die 2 "commits between HEAD and the candidate change the nginx or compose files: this is not a step-only deployment"
  local out; out=$(outside_scope "$PRE" "$CAND" "$STEP")
  [ -z "$out" ] || die 2 "the fast-forward would change files outside the approved scope (the files of step $STEP, docs, these tools and their tests):$out"
  out=$(recorded_step_of "$CAND"); [ -z "$out" ] || [ "$out" = "$STEP" ] || die 2 "${CAND:0:10} is recorded as step $out in $STATE_DIR, not as step $STEP"
  local obs; obs=$(observed_step)
  [ "$obs" = "$((STEP - 1))" ] || die 2 "step $STEP needs the checkout at step $((STEP - 1)), it is at step $obs"
  local f dirty=""
  for f in $(git diff --name-only HEAD); do git diff --quiet "$PRE" "$CAND" -- "$f" || dirty="$dirty $f"; done
  [ -z "$dirty" ] || die 2 "tracked files that the merge changes are modified locally:$dirty"
  for f in $(git diff --name-only --diff-filter=A "$PRE" "$CAND"); do [ -e "$f" ] && die 2 "$f exists in the checkout but is added by the candidate"; done
  local st
  st=$(cstate "$NGINX") || die 2 "container $NGINX not found"; case "$st" in "running|healthy") ;; *) die 2 "container $NGINX is $st (healthy expected)" ;; esac
  st=$(cstate "$CF") || die 2 "container $CF not found"; case "$st" in running\|*) ;; *) die 2 "container $CF is $st" ;; esac
  PROJECT=$(expected_project); [ -n "$PROJECT" ] || die 2 "cannot determine the Compose project"
  ORIG_NGINX_IMG=$(cfield "$NGINX" '{{.Image}}'); ORIG_CF_IMG=$(cfield "$CF" '{{.Image}}')
  [ -z "$(evidence_get NGINX_IMAGE_ID)" ] || { [ "$(evidence_get NGINX_IMAGE_ID)" = "$ORIG_NGINX_IMG" ] && [ "$(evidence_get CLOUDFLARED_IMAGE_ID)" = "$ORIG_CF_IMG" ]; } \
    || die 2 "the running images differ from the captured ones in $IMAGES_ENV: R2 changes no image"
  mkdir -p "$TMP/cand"; git archive "$CAND" docker-compose.yml nginx/conf | tar -x -C "$TMP/cand" || die 2 "cannot extract the candidate"
  # the effective model of EVERY service, through the same Compose arguments as `up`
  hash_file "" "$TMP/cur.hash" || die 2 "docker compose config --hash fails for the CURRENT checkout: $(head -c 300 "$TMP/model.err" | tr '\n' ' ')"
  hash_file "$TMP/cand/docker-compose.yml" "$TMP/cand.hash" \
    || die 2 "docker compose config fails for the CANDIDATE with the arguments that up uses (is EDU_AI_TUNNEL_SUBNET set? step 0): $(head -c 300 "$TMP/model.err" | tr '\n' ' ')"
  [ "$(awk '{print $1}' "$TMP/cur.hash")" = "$(awk '{print $1}' "$TMP/cand.hash")" ] || die 2 "the set of services differs"
  local changed; changed=$(changed_services "$TMP/cur.hash" "$TMP/cand.hash")
  if [ "$STEP" = 2 ]; then
    [ "$changed" = "cloudflared nginx " ] || die 2 "the effective model must change exactly cloudflared and nginx, it changes: ${changed:-nothing}"
  else
    [ -z "$changed" ] || die 2 "step $STEP must not change the effective Compose model, it changes: $changed"
  fi
  # the hashes say WHICH services differ; this says WHAT differs: only the exact change of the step is accepted (paths are named, values never)
  model_pair "$TMP/cand/docker-compose.yml" | tool model-delta --step "$STEP" > "$TMP/delta.out" 2>&1 \
    || die 2 "the effective model changes more than step $STEP may: $(tr '\n' ' ' < "$TMP/delta.out" | cut -c1-600)"
  local img_c img_n
  if [ "$STEP" = 2 ]; then
    img_n=$(model_cand "$TMP/cand/docker-compose.yml" | tool image nginx); img_c=$(model_cand "$TMP/cand/docker-compose.yml" | tool image cloudflared)
    [ "$(docker image inspect --format '{{.Id}}' "$img_n" 2>/dev/null)" = "$ORIG_NGINX_IMG" ] || die 2 "the candidate nginx image '$img_n' is not the running image: R2 changes no image (the deploy never pulls)"
    [ "$(docker image inspect --format '{{.Id}}' "$img_c" 2>/dev/null)" = "$ORIG_CF_IMG" ] || die 2 "the candidate cloudflared image '$img_c' is not the running image: R2 changes no image"
    model_cand "$TMP/cand/docker-compose.yml" | tool model-check --conf-dir "$(abs "$TMP/cand/nginx/conf")" --allow-legacy > "$TMP/mc.out" \
      || die 2 "the candidate model is not the designed one: $(tr '\n' ' ' < "$TMP/mc.out")"
    SUBNET=$(model_cand "$TMP/cand/docker-compose.yml" | tool model-subnet) || die 2 "no subnet for the tunnel network in the candidate model"
    model_cand "$TMP/cand/docker-compose.yml" | tool render-trust > "$TMP/trust.conf" || die 2 "the candidate model has no trust config"
    [ "$(cnets "$NGINX")" = "edge" ] || die 2 "nginx is on '$(cnets "$NGINX")', step 2 starts from 'edge' (the S2 wiring)"
    [ "$(cnets "$CF")" = "app edge" ] || die 2 "cloudflared is on '$(cnets "$CF")', step 2 starts from 'app edge'"
    # the subnet: the preflight tool must say 0 and nothing else (any other status, a crash included, is a refusal)
    local args=(check "$SUBNET" --project "$PROJECT" --routes "$ROUTES_MODE") r
    [ "$ROUTES_MODE" = docker ] && args+=(--helper-image "$HELPER_IMAGE")
    for r in $RESERVED; do args+=(--reserved "$r"); done
    tool "${args[@]}" > "$TMP/subnet.out" 2>&1; local rc=$?
    [ "$rc" = 0 ] || die 2 "the subnet preflight for $SUBNET said $rc, not 0: $(tr '\n' ' ' < "$TMP/subnet.out" | cut -c1-600)"
    log "subnet $SUBNET: $(head -1 "$TMP/subnet.out" | cut -c1-120)"
    nginx_t_candidate "$TMP/cand/nginx/conf" "$TMP/trust.conf" || die 2 "nginx -t failed on the candidate"
    cf_ingress > "$TMP/ingress.now"
    if [ ! -s "$TMP/ingress.now" ]; then
      [ "$INGRESS_REQUIRED" = 1 ] && die 2 "no tunnel routes can be read from the log of $CF: the routes cannot be shown to be preserved"
    fi
  else
    nginx_t_candidate "$TMP/cand/nginx/conf" "" || die 2 "nginx -t failed on the candidate"
    if [ "$STEP" = 3 ]; then
      model_cur | tool model-check --conf-dir "$(abs "$TMP/cand/nginx/conf")" > "$TMP/mc.out" || die 2 "the candidate is not the designed final state: $(tr '\n' ' ' < "$TMP/mc.out")"
      SUBNET=$(model_cur | tool model-subnet) || die 2 "no tunnel subnet in the current model"
      model_cur | tool render-trust > "$TMP/trust.conf" || die 2 "no trust config in the current model"
      nginx_t_candidate "$TMP/cand/nginx/conf" "$TMP/trust.conf" || die 2 "nginx -t failed on the candidate with the trust file"
      [ "$(cnets "$NGINX")" = "edge tunnel" ] && [ "$(cnets "$CF")" = "app tunnel tunnel_egress" ] || die 2 "the wiring is not the step-2 wiring: nginx '$(cnets "$NGINX")', cloudflared '$(cnets "$CF")'"
      [ "$(docker exec "$NGINX" cat /etc/edu_ai/tunnel/trust.conf 2>/dev/null)" = "set_real_ip_from $SUBNET;" ] || die 2 "the running nginx does not carry the trust file of $SUBNET"
    else
      local d; d=$(conf_directives "$TMP/cand/nginx/conf/nginx.conf")
      [ "$(printf '%s\n' "$d" | grep -c 'include /etc/edu_ai/tunnel/\*\.conf;')" = 1 ] || die 2 "the candidate nginx.conf must include the trust file exactly once"
      printf '%s\n' "$d" | grep -qE "set_real_ip_from[[:space:]]+${LEGACY_RANGE//./\\.};" || die 2 "step 1 keeps the old trusted range, the candidate lost it"
      printf '%s\n' "$d" | grep -q 'proxy_add_x_forwarded_for' && die 2 "the candidate still appends to a caller-supplied X-Forwarded-For"
    fi
    cf_ingress > "$TMP/ingress.now"
  fi
  local bad
  bad=$(smoke_once) || die 2 "the CURRENT nginx fails the smoke list before any change:$bad"
  log "preflight OK: step $STEP, $PRE -> $CAND, project $PROJECT${SUBNET:+, subnet $SUBNET}, effective model changes: ${changed:-none}, current smoke OK"
  phase preflight
}

# ── git: the return of a commit, limited to that commit ───────────────────────────────────────
revert_commit() {
  mk_tmp
  git merge-base --is-ancestor "$CAND" HEAD 2>/dev/null || { log "the commit ${CAND:0:10} is not in HEAD: nothing to revert"; return 0; }
  local wt="$TMP/revert-wt" out new
  git worktree add -q --detach "$wt" HEAD >> "$LOG" 2>&1 || { log "cannot create the scratch worktree"; return 1; }
  if out=$(git -C "$wt" -c user.name=r2-rollback -c user.email=r2-rollback@localhost -c commit.gpgsign=false revert --no-edit "$CAND" 2>&1); then
    new=$(git -C "$wt" rev-parse HEAD)
    git worktree remove --force "$wt" >> "$LOG" 2>&1 || log "the scratch worktree $wt could not be removed; remove it with: git worktree remove --force $wt"
    if out=$(git merge --ff-only "$new" 2>&1); then return 0; fi
    log "the revert commit could not be fast-forwarded into this checkout (local changes in files it touches): $(printf '%s' "$out" | head -3 | tr '\n' ' ')"
    return 1
  fi
  log "git revert not possible: $(printf '%s' "$out" | head -3 | tr '\n' ' ')"
  git worktree remove --force "$wt" >> "$LOG" 2>&1 || log "the scratch worktree $wt could not be removed; remove it with: git worktree remove --force $wt"
  return 1
}
# without git history: put the saved pre-step files back, only where the file still is exactly the candidate's version.
# R2_FORCE_RESTORE=1 is the owner's decision to overwrite a file that something else changed (the S2 script's own `rollback` does that to
# docker-compose.yml): the file as it is now is kept in $STATE_DIR/overwritten, and the tree stays modified relative to HEAD.
restore_from_bundle() {
  local n=$1 f src force=${R2_FORCE_RESTORE:-0}
  for f in $(step_files "$n"); do
    src="$STATE_DIR/bundle/step$n/$f"
    [ -f "$src" ] || { log "no saved pre-step copy of $f in $STATE_DIR/bundle/step$n"; return 1; }
    [ "$force" = 1 ] || git show "$CAND:$f" 2>/dev/null | cmp -s - "$f" \
      || { log "$f is not the version of the step commit any more: it is not overwritten (R2_FORCE_RESTORE=1 overwrites it with the saved pre-step copy and keeps this file in $STATE_DIR/overwritten)"; return 1; }
  done
  for f in $(step_files "$n"); do
    if [ "$force" = 1 ]; then mkdir -p "$STATE_DIR/overwritten/step$n/$(dirname "$f")"; cp -p "$f" "$STATE_DIR/overwritten/step$n/$f"; fi
    cat "$STATE_DIR/bundle/step$n/$f" > "$f"; log "restored $f from the saved pre-step copy (the tree is now modified relative to HEAD)"
  done
}

# ── what a step must have achieved ───────────────────────────────────────────────────────────
new_workers_only() {    # $1 worker PIDs before, $2 after: true when there are workers now and none of them existed before the reload
  local p; [ -n "$2" ] || return 1
  for p in $2; do case " $1 " in *" $p "*) return 1 ;; esac; done
  return 0
}
reload_nginx() {        # nginx -t and a reload in the RUNNING container; the reload must have started new workers
  local before after deadline
  docker exec "$NGINX" nginx -t -g 'daemon off;' > "$TMP/t.out" 2>&1 || { log "nginx -t in the running container failed: $(tr '\n' ' ' < "$TMP/t.out" | cut -c1-300)"; return 1; }
  before=$(nginx_workers)
  docker exec "$NGINX" nginx -s reload >> "$LOG" 2>&1 || { log "nginx -s reload failed"; return 1; }
  deadline=$(( $(date +%s) + RELOAD_DEADLINE ))
  while :; do
    after=$(nginx_workers)
    if new_workers_only "$before" "$after"; then return 0; fi
    [ "$(date +%s)" -ge "$deadline" ] && { log "no new nginx workers within ${RELOAD_DEADLINE}s after the reload (before: $before, after: ${after:-none})"; return 1; }
    sleep 1
  done
}
conf_loaded_has() {     # $1 extended regex that must (rc 0) appear in the configuration nginx is started with
  docker exec "$NGINX" nginx -T 2>/dev/null | grep -vE '^[[:space:]]*#' | grep -qE "$1"
}
same_container() {      # $1 name, $2 recorded "id started" : a reload must not replace the container
  [ "$(cfield "$1" '{{.Id}} {{.State.StartedAt}}')" = "$2" ]
}

verify_step1() {
  conf_loaded_has 'include /etc/edu_ai/tunnel/\*\.conf;' || { log "the loaded configuration does not include the trust file"; return 1; }
  conf_loaded_has "set_real_ip_from[[:space:]]+${LEGACY_RANGE//./\\.};" || { log "the old trusted range is not in the loaded configuration"; return 1; }
  ! conf_loaded_has 'proxy_add_x_forwarded_for' || { log "the loaded configuration still appends to X-Forwarded-For"; return 1; }
}
verify_nginx_step2() {
  [ "$(cfield "$NGINX" '{{.Image}}')" = "$ORIG_NGINX_IMG" ] || { log "the new nginx does not run the captured image $ORIG_NGINX_IMG"; return 1; }
  [ "$(cnets "$NGINX")" = "edge tunnel" ] || { log "the new nginx is on '$(cnets "$NGINX")', expected 'edge tunnel'"; return 1; }
  [ "$(docker exec "$NGINX" cat /etc/edu_ai/tunnel/trust.conf 2>/dev/null)" = "set_real_ip_from $SUBNET;" ] || { log "the trust file in the new nginx is not 'set_real_ip_from $SUBNET;'"; return 1; }
  conf_loaded_has "set_real_ip_from[[:space:]]+${SUBNET//./\\.};" && conf_loaded_has "set_real_ip_from[[:space:]]+${LEGACY_RANGE//./\\.};" || { log "the loaded configuration does not trust both the new subnet and the old range"; return 1; }
  wait_ready 1 || return 1
  smoke || return 1
  want_probe tunnel HONOURED "the tunnel network is trusted"
}
verify_cloudflared_step2() {
  [ "$(cstate "$CF" | cut -d'|' -f1)" = running ] || { log "cloudflared is not running"; return 1; }
  [ "$(cfield "$CF" '{{.Image}}')" = "$ORIG_CF_IMG" ] || { log "the new cloudflared does not run the captured image $ORIG_CF_IMG"; return 1; }
  [ "$(cnets "$CF")" = "app tunnel tunnel_egress" ] || { log "the new cloudflared is on '$(cnets "$CF")', expected 'app tunnel tunnel_egress'"; return 1; }
  wait_ingress_same || return 1
  targets_reachable || return 1
  want_probe tunnel HONOURED "a member of the tunnel network is trusted" || return 1
  others_unchanged || { log "another container of the project changed"; return 1; }
}
verify_step3() {
  ! conf_loaded_has "set_real_ip_from[[:space:]]+${LEGACY_RANGE//./\\.};" || { log "the old trusted range is still in the loaded configuration"; return 1; }
  conf_loaded_has "set_real_ip_from[[:space:]]+${SUBNET//./\\.};" || { log "the loaded configuration lost the tunnel subnet"; return 1; }
  smoke || return 1
  want_probe tunnel HONOURED "the tunnel network is still trusted" || return 1
  want_probe edge IGNORED "a member of edge is not trusted any more" || return 1
  targets_reachable || return 1
}

# ── the steps ────────────────────────────────────────────────────────────────────────────────
step_conf() {           # steps 1 and 3: the files changed by the merge are seen through the directory mount; reload, no container change
  local rec; rec=$(cfield "$NGINX" '{{.Id}} {{.State.StartedAt}}'); local cfrec; cfrec=$(cfield "$CF" '{{.Id}} {{.State.StartedAt}}')
  reload_nginx || fail "the reload did not take effect"
  phase reloaded
  same_container "$NGINX" "$rec" || fail "the nginx container was replaced during a reload-only step"
  same_container "$CF" "$cfrec" || fail "the cloudflared container changed during a reload-only step"
  if [ "$STEP" = 1 ]; then verify_step1 || fail "the loaded configuration is not the step-1 configuration"; else verify_step3 || fail "step 3 checks failed"; fi
  wait_ready 0 || fail "nginx is not healthy after the reload"
  smoke || fail "smoke failed after the reload"
}
step_compose() {        # step 2
  write_pin "$ORIG_NGINX_IMG" "$ORIG_CF_IMG" || fail "cannot write the image pin"
  log "recreating $NGINX first (compose up --no-deps --pull never nginx on image ID $ORIG_NGINX_IMG, deadline ${UP_DEADLINE}s): cloudflared is still on edge and the old range is still trusted, so the public path keeps working"
  up_logged "$UP_DEADLINE" up -d --no-deps --pull never nginx || fail "docker compose up nginx failed or exceeded ${UP_DEADLINE}s"
  phase nginx_recreated
  verify_nginx_step2 || fail "the recreated nginx failed its checks"
  log "recreating $CF (image ID $ORIG_CF_IMG): it moves from edge to tunnel + tunnel_egress"
  up_logged "$UP_DEADLINE" up -d --no-deps --pull never cloudflared || fail "docker compose up cloudflared failed or exceeded ${UP_DEADLINE}s"
  phase cloudflared_recreated
  verify_cloudflared_step2 || fail "the recreated cloudflared failed its checks"
}
apply() {
  FORWARD=1
  preflight "$1" "$2"
  save_image_evidence
  mkdir -p "$STATE_DIR/bundle/step$STEP"
  local f
  for f in $(step_files "$STEP"); do mkdir -p "$STATE_DIR/bundle/step$STEP/$(dirname "$f")"; git show "$PRE:$f" > "$STATE_DIR/bundle/step$STEP/$f" || die 2 "cannot save $f"; done
  project_ids | grep -v -e "^/$NGINX " -e "^/$CF " > "$STATE_DIR/ids.before"
  # the routes the running tunnel reports now are what the step must preserve (step 2 re-reads them; the others keep the first record)
  if [ -s "$TMP/ingress.now" ] && { [ "$STEP" = 2 ] || [ ! -s "$STATE_DIR/ingress.before" ]; }; then cp "$TMP/ingress.now" "$STATE_DIR/ingress.before"; fi
  [ -f "$STATE_DIR/ingress.before" ] || : > "$STATE_DIR/ingress.before"
  printf '%s %s %s\n' "$STEP" "$CAND" "$PRE" >> "$STATE_DIR/steps.log"
  trap '' HUP INT TERM        # from here an SSH drop or Ctrl-C must not stop the step half-way (before this point nothing was changed)
  git merge --ff-only "$CAND" >> "$LOG" 2>&1 || die 3 "fast-forward failed; the checkout was not changed"
  phase merged
  git diff --quiet "$CAND" -- docker-compose.yml nginx/conf && diff -r "$TMP/cand/nginx/conf" nginx/conf > /dev/null 2>&1 || fail "the checkout differs from the validated candidate"
  hash_file "" "$TMP/after.hash" || fail "docker compose config fails after the merge"
  diff "$TMP/cand.hash" "$TMP/after.hash" > /dev/null || fail "the effective model after the merge differs from the validated one"
  local t0 t1; t0=$(now)
  if [ "$STEP" = 2 ]; then step_compose; else step_conf; fi
  others_unchanged || fail "another container of the project changed"
  phase verified
  t1=$(now)
  log "R2_RESULT status=ok step=$STEP seconds=$(secs "$t0" "$t1") from=$PRE to=$CAND"
}

# ── return ───────────────────────────────────────────────────────────────────────────────────
# Removes the containers of THIS project and service that a killed `up` left behind; a container of another project or service that
# holds the name is a NAME COLLISION: it is kept and the return fails.
clear_service_containers() {  # $1 service $2 container name
  local ids id holder lbl
  ids=$(compose ps -a -q "$1" 2>/dev/null)
  for id in $ids; do
    lbl=$(cfield "$id" '{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}')
    [ "$lbl" = "$PROJECT|$1" ] || { log "NAME COLLISION: container ${id:0:12} is listed for $1 but its labels are not project '$PROJECT' / service $1; NOT removed"; return 1; }
    log "removing container ${id:0:12} (project $PROJECT, service $1)"; docker rm -f "$id" >> "$LOG" 2>&1 || true
  done
  holder=$(cfield "$2" '{{.Id}}') || return 0
  lbl=$(cfield "$holder" '{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}')
  if [ "$lbl" = "$PROJECT|$1" ]; then log "removing container ${holder:0:12} that holds the name $2"; docker rm -f "$holder" >> "$LOG" 2>&1 || true; return 0; fi
  log "NAME COLLISION: container $2 (${holder:0:12}, labels '$lbl') holds the name but is not project '$PROJECT' / service $1; NOT removed"; return 1
}
up_service_with_retry() {     # $1 service $2 container name
  local try
  for try in 1 2; do
    if up_logged "$RETURN_UP_DEADLINE" up -d --no-deps --pull never "$1"; then
      case "$(cstate "$2")" in running\|*|restarting\|*) return 0 ;; esac
      log "$2 is not running after up ($(cstate "$2" || echo missing))"
    else
      log "docker compose up $1 failed during the return"
    fi
    [ "$try" = 2 ] && return 1
    log "removing the $1 containers of the project (by labels) and trying once more"
    clear_service_containers "$1" "$2" || return 1
  done
}
remove_tunnel_networks() {    # only networks of THIS project that are the R2 networks and have no endpoint
  local n full lbl cnt
  for n in tunnel tunnel_egress; do
    full="${PROJECT}_$n"
    docker network inspect "$full" > /dev/null 2>&1 || continue
    lbl=$(docker network inspect --format '{{index .Labels "com.docker.compose.project"}}|{{index .Labels "com.docker.compose.network"}}' "$full" 2>/dev/null)
    cnt=$(docker network inspect --format '{{len .Containers}}' "$full" 2>/dev/null)
    if [ "$lbl" = "$PROJECT|$n" ] && [ "${cnt:-1}" = 0 ]; then docker network rm "$full" >> "$LOG" 2>&1 && log "removed the network $full" || log "could not remove $full"
    else log "the network $full is kept (labels '$lbl', endpoints ${cnt:-?})"; fi
  done
}
return_conf_step() {          # steps 1 and 3
  local rec; rec=$(cfield "$NGINX" '{{.Id}} {{.State.StartedAt}}')
  [ -n "$rec" ] || { log "container $NGINX does not exist"; return 1; }
  reload_nginx || return 1
  phase returned_reload
  same_container "$NGINX" "$rec" || { log "the nginx container was replaced"; return 1; }
  if [ "$1" = 1 ]; then
    ! conf_loaded_has 'include /etc/edu_ai/tunnel/\*\.conf;' || { log "the loaded configuration still includes the trust file"; return 1; }
    conf_loaded_has 'proxy_add_x_forwarded_for' || { log "the loaded configuration does not append to X-Forwarded-For again"; return 1; }
  else
    conf_loaded_has "set_real_ip_from[[:space:]]+${LEGACY_RANGE//./\\.};" || { log "the old trusted range is not back in the loaded configuration"; return 1; }
  fi
  wait_ready 0 || return 1
  smoke || return 1
}
return_compose_step() {       # step 2: the S2 wiring of BOTH services comes back, on the captured image IDs
  load_evidence || return 1
  [ -s "$STATE_DIR/ingress.before" ] || log "no saved tunnel routes: the routes are not compared after the return"
  log "recreating $CF on the previous wiring (app, edge); nginx is still on edge and trusts the old range"
  up_service_with_retry cloudflared "$CF" || return 1
  phase returned_cloudflared
  log "recreating $NGINX on the S2 definition (edge only, no trust file)"
  up_service_with_retry nginx "$NGINX" || return 1
  phase returned_nginx
  if [ "$(cfield "$NGINX" '{{.Image}}')" != "$ORIG_NGINX_IMG" ] || [ "$(cfield "$CF" '{{.Image}}')" != "$ORIG_CF_IMG" ]; then log "a returned container does not run its captured image"; return 1; fi
  wait_ready 0 || return 1
  remove_tunnel_networks
  [ "$(cnets "$NGINX")" = "edge" ] || { log "nginx is on '$(cnets "$NGINX")' after the return, expected 'edge'"; return 1; }
  [ "$(cnets "$CF")" = "app edge" ] || { log "cloudflared is on '$(cnets "$CF")' after the return, expected 'app edge'"; return 1; }
  ! docker exec "$NGINX" test -e /etc/edu_ai/tunnel/trust.conf 2>/dev/null || { log "the trust file is still mounted in nginx"; return 1; }
  smoke || return 1
  if [ -s "$STATE_DIR/ingress.before" ]; then wait_ingress_same || return 1; targets_reachable || return 1; fi
  others_unchanged || { log "another container of the project changed"; return 1; }
}
rollback_step() {             # $1 step; CAND is the commit of that step (checked by the callers; the step and the ancestry are checked here too)
  local n=$1 obs
  case "$n" in 1|2|3) ;; *) log "the step must be 1, 2 or 3 (got '$n'): nothing is returned"; return 1 ;; esac
  trap '' HUP INT TERM
  log "returning step $n (only the commit of this step is undone)"
  phase returning
  [ -n "$PROJECT" ] || PROJECT=$(expected_project)
  [ -n "$PROJECT" ] || { log "cannot determine the Compose project"; return 1; }
  [ -f "$STATE_DIR/ids.before" ] || project_ids | grep -v -e "^/$NGINX " -e "^/$CF " > "$STATE_DIR/ids.before"
  mk_tmp
  obs=$(observed_step)
  if [ "$obs" -ge "$n" ]; then
    [ "$obs" = "$n" ] || { log "step $n cannot be returned while the checkout is at step $obs: return step $obs first"; return 1; }
    git merge-base --is-ancestor "$CAND" HEAD || { log "the checkout is at step $n but ${CAND:0:10} is not in HEAD: nothing is reverted"; return 1; }
    revert_commit || restore_from_bundle "$n" || return 1
  else
    log "the files of step $n are not in the checkout (already returned, or never merged): only the running state is made to match"
  fi
  case "$n" in 1|3) return_conf_step "$n" || return 1 ;; 2) return_compose_step || return 1 ;; esac
  if [ -f "$STATE_DIR/steps.log" ]; then grep -v "^$n " "$STATE_DIR/steps.log" > "$STATE_DIR/steps.log.new"; mv -f "$STATE_DIR/steps.log.new" "$STATE_DIR/steps.log"; fi
  log "R2_RESULT status=returned step=$n"
  return 0
}
fail() {
  log "FAIL: $1"
  FORWARD=0
  if [ "$AUTO_ROLLBACK" = 1 ]; then
    if rollback_step "$STEP"; then exit 4; fi
    log "THE RETURN FAILED: ingress may be down. Fix the cause shown above, then run: $0 rollback $STEP $CAND"
    exit 5
  fi
  log "left as is (R2_AUTO_ROLLBACK=0); to return run: $0 rollback $STEP $CAND"
  exit 3
}

status() {
  PROJECT=$(expected_project)
  local obs nn cn trust
  obs=$(observed_step); nn=$(cnets "$NGINX"); cn=$(cnets "$CF")
  echo "checkout step: $obs   project: ${PROJECT:-?}   HEAD: $(git rev-parse --short HEAD)"
  echo "nginx:        $(cstate "$NGINX" || echo missing)  networks: $nn  image: $(cfield "$NGINX" '{{.Image}}')"
  echo "cloudflared:  $(cstate "$CF" || echo missing)  networks: $cn  image: $(cfield "$CF" '{{.Image}}')"
  # the two services must agree on the hop between them: cloudflared reaches nginx only over a network that both are on
  trust=no; docker exec "$NGINX" test -e /etc/edu_ai/tunnel/trust.conf 2>/dev/null && trust=yes
  case "$nn|$cn|$trust" in
    "edge|app edge|no"|"edge tunnel|app tunnel tunnel_egress|yes"|"edge tunnel|app edge|yes") echo "wiring: consistent" ;;
    *) echo "wiring: INCONSISTENT (nginx '$nn', trust file $trust, cloudflared '$cn'): the tunnel may be down. Return R2 (rollback-all), do not use the S2 script" ;;
  esac
  [ "$obs" = 0 ] || echo "NOTE: while a step is applied return S2 only through scripts/nginx_s2_guard.sh (it refuses until rollback-all is done). The S2 script run directly recreates nginx on the S2 definition and leaves cloudflared on tunnel (exit 0, smoke OK, tunnel down)."
  echo "routes:"; cf_ingress | sed 's/^/  /'
  echo "steps recorded in the state directory (step commit parent): $(tr '\n' ';' < "$STATE_DIR/steps.log" 2>/dev/null)"
}

main() {
  local cmd=${1:-} step=${2:-} sha=${3:-}
  case "$cmd" in
    smoke|status|rollback-all) ;;
    preflight|apply|rollback) [ -n "$step" ] && [ -n "$sha" ] || { sed -n '2,10p' "$0"; exit 2; } ;;
    *) sed -n '2,10p' "$0"; exit 2 ;;
  esac
  load_compose_args
  check_default_mode
  mk_tmp
  echo "$$" > "$STATE_DIR/pid"
  case "$cmd" in
    smoke)     smoke && exit 0; exit 2 ;;
    status)    status ;;
    preflight) preflight "$step" "$sha"; log "preflight only: nothing was changed" ;;
    apply)     apply "$step" "$sha" ;;
    rollback)       # the step and the commit are judged before ANY change (checkout, containers, state directory)
      STEP=$step
      validate_return_commit "$STEP" "$sha" && validate_return_state "$STEP" || die 2 "refused before any change: '$step' '$sha' is not a valid step and commit to return now"
      if rollback_step "$STEP"; then exit 0; fi
      log "THE RETURN FAILED: ingress may be down. Fix the cause shown above, then run it again: $0 rollback $STEP $CAND"
      exit 5 ;;
    rollback-all)
      [ -s "$STATE_DIR/steps.log" ] || { log "no step is recorded in $STATE_DIR: nothing to return (use: $0 rollback <step> <SHA>)"; exit 0; }
      local s c
      for s in 3 2 1; do     # every recorded step is judged first: a damaged record changes nothing
        c=$(recorded_commit "$s"); [ -n "$c" ] || continue
        validate_return_commit "$s" "$c" || die 2 "refused before any change: the record of step $s in $STATE_DIR is not the commit of that step"
      done
      for s in 3 2 1; do
        c=$(recorded_commit "$s"); [ -n "$c" ] || continue
        STEP=$s; CAND=$(git rev-parse "$c^{commit}")
        rollback_step "$STEP" || { log "THE RETURN FAILED at step $s: fix the cause shown above, then run: $0 rollback-all"; exit 5; }
      done
      exit 0 ;;
  esac
  exit 0
}

if [ -z "${R2_SOURCE_ONLY:-}" ]; then main "$@"; fi
