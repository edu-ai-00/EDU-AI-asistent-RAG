#!/bin/bash
# usage: acc_snap.sh LABEL  -> ~/acc/rec/snap_<label>.txt  (hashes, ids, counts; never secrets)
. $HOME/acc/tools/acc_lib.sh
O=$R/snap_$1.txt
{
echo "env_sha256 $(sha256sum $CFG/.env | cut -c1-16)"
echo "secret_key_sha256 $(grep ^LANGFLOW_SECRET_KEY= $CFG/.env | cut -d= -f2- | tr -d '\n' | sha256sum | cut -c1-16)"
echo "api_key_sha256 $(grep ^LANGFLOW_API_KEY= $CFG/.env | cut -d= -f2- | tr -d '\n' | sha256sum | cut -c1-16)"
echo "install_record_sha $(sha256sum $CFG/install-record.json | cut -c1-16)"
echo "edu_ai_image $(docker inspect edu_ai --format '{{.Image}} {{.Config.Image}}' 2>/dev/null)"
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
echo "bifrost_providers $(curl -s -m 8 http://127.0.0.1:8099/api/providers | python3 -c 'import sys,json; d=json.load(sys.stdin); print(sorted(p["name"] for p in d["providers"]))' 2>&1)"
echo "langflow_data_files:"
(cd $ST/langflow_data && sudo find . -type f ! -name '*.log' ! -path './logs/*' ! -path './profile_pictures/*' ! -name '*.pyc' -printf '%P\n' | sort | while read f; do echo "$(sudo sha256sum "$f" | cut -c1-12) $f"; done)
echo "bifrost_data_files:"; (cd $ST/bifrost_data && sudo find . -type f ! -name '*.log' -printf '%P %s\n' | sort)
echo "uploads_files:"; (cd $ST/uploads && sudo find . -type f -printf '%P\n' | sort | while read f; do echo "$(sudo sha256sum "$f" | cut -c1-12) $f"; done)
echo "sqllite_export_files:"; (cd $ST/sqllite_export && sudo find . -type f -printf '%P\n' | sort | while read f; do echo "$(sudo sha256sum "$f" | cut -c1-12) $f"; done)
echo "volumes: $(docker volume ls -q --filter label=com.docker.compose.project=$PRJ | sort | tr '\n' ' ')"
} > $O 2>&1
cut -c1-220 $O
