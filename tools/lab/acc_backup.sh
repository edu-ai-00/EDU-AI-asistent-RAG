#!/bin/bash
# usage: acc_backup.sh NAME   (stack running; writers are stopped for the backup window, then restarted)
# Consistent backup: both databases as edu_ai_admin (custom format), globals (roles + grants), role evidence,
# config (.env, install-record) and state (uploads, langflow_data incl. secret_key, sqllite_export, bifrost_data), image manifest.
. $HOME/acc/tools/acc_lib.sh
NAME=$1; B=$A/backup/$NAME; mkdir -p $B && chmod 700 $B $A/backup
cd $SRC
mark "backup $NAME" "START source=$(git -c safe.directory='*' rev-parse HEAD)"
python3 scripts/db_roles.py verify --container edu_ai_postgres > $B/role_verify_at_backup.txt 2>&1; echo "role verify rc=$? : $(tail -1 $B/role_verify_at_backup.txt | cut -c1-120)"
python3 scripts/db_roles.py globals --container edu_ai_postgres --out $B/globals.sql | tail -2
SV=$($DC config --services | grep -v -e cloudflared -e '^postgres$' | tr '\n' ' ')
$DC stop $SV >/dev/null 2>&1 < /dev/null; mark "backup $NAME" "writers stopped: $SV"
for db in edu_ai langflow; do docker exec edu_ai_postgres pg_dump -U $PGU -Fc -d $db > $B/$db.dump || { mark "backup $NAME" "pg_dump $db FAILED"; exit 1; }; done
mark "backup $NAME" "pg_dump edu_ai + langflow done"
$DC stop postgres >/dev/null 2>&1 < /dev/null
tar -C $A -czf $B/config.tgz config
sudo tar --numeric-owner -p -C $A -czf $B/state.tgz state
sudo chown $USER:$USER $B/*; chmod 600 $B/*
mark "backup $NAME" "config + state archived"
{
echo "backup $NAME created $(date -u +%FT%TZ)"
echo "source_sha $(git -c safe.directory='*' rev-parse HEAD)"
echo "images (repo:tag id):"; docker images --no-trunc --format '{{.Repository}}:{{.Tag}} {{.ID}}' | grep -E '^(edu_ai|maximhq|nginx|dpage|cloudflare)' | sort
echo "container_images:"; docker ps -a --filter label=com.docker.compose.project=$PRJ --format '{{.Names}} {{.Image}}' | sort
echo "volumes: $(docker volume ls -q --filter label=com.docker.compose.project=$PRJ | sort | tr '\n' ' ')"
docker start edu_ai_postgres >/dev/null 2>&1; for i in $(seq 1 30); do docker exec edu_ai_postgres pg_isready -h 127.0.0.1 -U $PGU -q 2>/dev/null && break; sleep 1; done
echo "lf_alembic_head $($PL -c 'select version_num from alembic_version' | tr '\n' ',')"
} > $B/META.txt
for db in edu_ai langflow; do n=$(docker exec -i edu_ai_postgres pg_restore --list < $B/$db.dump | grep -c 'TABLE DATA'); echo "$db dump TABLE DATA entries: $n" >> $B/META.txt; done
(cd $B && sha256sum *.dump *.tgz META.txt globals.sql role_verify_at_backup.txt > SHA256SUMS)
mark "backup $NAME" "checksums + META written"
$DC up -d --wait $SV 2>&1 < /dev/null | tail -2; mark "backup $NAME" "writers restarted (backup window end)"
(cd $B && sha256sum -c SHA256SUMS | tr '\n' ' '); echo; du -sh $B | cut -f1; ls -l $B | awk '{print $1, $5, $9}'
