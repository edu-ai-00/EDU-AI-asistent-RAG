# Shared variables for the acceptance scripts (source this; runs inside the sandbox: `sbx.sh bash script`).
A=$HOME/acc; R=$A/rec; SRC=${ACC_SRC:-$A/src}; CFG=$A/config; ST=$A/state; PRJ=edu_ai_acc; PGU=edu_ai_admin
T=$A/tools; L=$R/timeline.txt; mkdir -p $R
LAYOUT="--source-root $SRC --config-dir $CFG --state-dir $ST --project $PRJ"
DC="docker compose -p $PRJ --project-directory $SRC --env-file $CFG/.env"
P="docker exec edu_ai_postgres psql -U $PGU -d edu_ai -At"
PL="docker exec edu_ai_postgres psql -U $PGU -d langflow -At"
t0=$(date +%s)
mark(){ echo "$(date -u +%T) +$(( $(date +%s)-t0 ))s  [$1] $2" | tee -a $L; }
envval(){ grep -E "^$1=" $CFG/.env | head -1 | cut -d= -f2-; }
