#!/bin/bash
# usage: acc_restore.sh NAME EXPECTED_SNAPSHOT_LABEL
# FULL-LOSS restore inside the sandbox: down -v, config and state deleted, then restored from backup NAME.
# Order: unpack -> postgres only -> pg_restore both DBs -> db_roles reapply+verify -> RAW baseline before any application server ->
# services. Only the own project (edu_ai_acc) and the own directories are touched.
. $HOME/acc/tools/acc_lib.sh
NAME=$1; EXP=$R/snap_$2.txt; BK=$A/backup/$NAME
cd $SRC
mark "restore $NAME" "START (compose project $PRJ)"
(cd $BK && sha256sum -c SHA256SUMS | tr '\n' ' '); echo
$DC down -v >/dev/null 2>&1 < /dev/null
rm -rf $CFG; sudo rm -rf $ST; find $SRC -maxdepth 1 -type l -exec rm -f {} \;
mark "restore $NAME" "destroyed: project containers left: $(docker ps -a --filter label=com.docker.compose.project=$PRJ --format '{{.Names}}' | tr '\n' ' ')| volumes left: $(docker volume ls -q --filter label=com.docker.compose.project=$PRJ | tr '\n' ' ')"
tar -C $A -xzf $BK/config.tgz; sudo tar --numeric-owner -p -C $A -xzf $BK/state.tgz
for d in uploads langflow_data sqllite_export bifrost_data; do ln -s $ST/$d $SRC/$d; done; ln -s $CFG/.env $SRC/.env
mark "restore $NAME" "config + state restored (.env sha $(sha256sum $CFG/.env | cut -c1-16)); links recreated"
$DC up -d postgres >/dev/null 2>&1 < /dev/null
for i in $(seq 1 90); do docker exec edu_ai_postgres pg_isready -h 127.0.0.1 -U $PGU -q 2>/dev/null && break; sleep 2; done; mark "restore $NAME" "postgres accepts TCP"
for db in edu_ai langflow; do docker exec -i edu_ai_postgres pg_restore -U $PGU -d $db --clean --if-exists --no-owner --exit-on-error < $BK/$db.dump > $R/restore_${NAME}_pg_$db.txt 2>&1; echo "pg_restore $db rc=$?"; done; mark "restore $NAME" "pg_restore done"
python3 scripts/db_roles.py reapply --container edu_ai_postgres --confirm-isolated | tee $R/restore_${NAME}_roles.txt | tail -3
python3 scripts/db_roles.py verify --container edu_ai_postgres | tee -a $R/restore_${NAME}_roles.txt | tail -2
mark "restore $NAME" "role contract re-applied and verified before any application server"
echo "== RAW baseline before any application server (running: $(docker ps --format '{{.Names}}' | tr '\n' ' '))"
{
echo "qa_entries $($P -c 'select count(*) from edu_ai.qa_entries')"
echo "qa_variants $($P -c 'select count(*) from edu_ai.qa_variants')"
echo "qa_pack_manifest $($P -c 'select count(*) from edu_ai.qa_pack_manifest')"
echo "legal_entity $($P -c 'select count(*) from edu_ai.legal_entity')"
echo "staff_user $($P -c 'select count(*) from edu_ai.staff_user')"
echo "settings_active $($P -c 'select count(*) from edu_ai.settings where is_active')"
echo "document_chunks $($P -c 'select count(*) from edu_ai.document_chunks' 2>/dev/null | tail -1)"
echo "lf_alembic_head $($PL -c 'select version_num from alembic_version' | tr '\n' ',')"
echo "lf_users $($PL -c 'select count(*) from "user"') flows $($PL -c 'select count(*) from flow') variables $($PL -c 'select count(*) from variable') apikeys $($PL -c 'select count(*) from apikey')"
echo "lf_tables $($PL -c "select count(*) from information_schema.tables where table_schema='public'")"
echo "lf_flows_sig:"
$PL -F'|' -c "select coalesce(fl.endpoint_name,'<draft>'), fl.name, md5(fl.data::text), jsonb_array_length(fl.data::jsonb->'nodes') from flow fl join folder f on f.id=fl.folder_id where f.name <> 'Starter Projects' order by 1,2"
echo "lf_vendor_starters $($PL -F'|' -c "select count(*), md5(string_agg(md5(fl.data::text), ',' order by fl.name)) from flow fl join folder f on f.id=fl.folder_id where f.name = 'Starter Projects'")"
echo "lf_vars:"; $PL -F'|' -c "select name, type from variable order by 1"
} > $R/restore_${NAME}_raw.txt
sed -n '/^qa_entries/,/^bifrost_providers/p' $EXP | grep -v "^bifrost_providers" > $R/restore_${NAME}_expected.txt
diff $R/restore_${NAME}_expected.txt $R/restore_${NAME}_raw.txt && echo "RAW RESTORE == expected state (counts, alembic head, every flow md5, drafts, variables)"
echo "markers after restore (must be 0): db=$($P -c "select count(*) from edu_ai.ingestion_log where component_name='RPO_MARKER_ACC'") flow=$($PL -c "select count(*) from flow where name like 'ACC MARKER FLOW%'")"
mark "restore $NAME" "raw baseline compared before ASGI"
$DC up -d --wait $($DC config --services | grep -v -e '^postgres$' -e cloudflared | tr '\n' ' ') 2>&1 < /dev/null | tail -2
mark "restore $NAME" "services healthy; edu_ai image $(docker inspect edu_ai --format '{{.Config.Image}} {{.Image}}' | cut -c1-80)"
