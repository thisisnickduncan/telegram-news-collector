#!/usr/bin/env bash
# Push local application code to the news-alert VM and restart the service.
#
# Usage:
#   NEWS_ALERT_HOST=opc@203.0.113.10 ./deploy/deploy.sh
#   ./deploy/deploy.sh --migrate          # also run scripts/migrate_v2.py
#   ./deploy/deploy.sh --seed-bias        # also refresh the AllSides/MBFC bias data
#
# Connection settings come from the environment, or from deploy/target.env if that
# file exists (it is gitignored -- the host address is deployment config, not source).
#
# The step order is not arbitrary and changing it breaks things in ways that are quiet
# rather than loud:
#
#   * stop before uploading, so a schema migration's ALTER TABLEs aren't racing a live
#     poller, and so a half-uploaded package never gets imported;
#   * migrate before starting, because new code reads columns that don't exist yet;
#   * restorecon after moving files into place. This is the one that bites. Files
#     uploaded to /tmp carry the SELinux type user_tmp_t, and they keep it when copied
#     into /opt -- systemd's confined service then cannot read its own source and the
#     unit fails to start with an error that looks nothing like a labeling problem.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

[ -f deploy/target.env ] && . deploy/target.env

RUN_MIGRATE=0
RUN_SEED_BIAS=0
for arg in "$@"; do
    case "$arg" in
        --migrate)   RUN_MIGRATE=1 ;;
        --seed-bias) RUN_SEED_BIAS=1 ;;
        -h|--help)   sed -n '2,12p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

if [ -z "${NEWS_ALERT_HOST:-}" ]; then
    cat >&2 <<'EOF'
NEWS_ALERT_HOST is not set.

Set it for one run:
    NEWS_ALERT_HOST=opc@<vm-address> ./deploy/deploy.sh

or persist it (this path is gitignored):
    echo 'NEWS_ALERT_HOST=opc@<vm-address>' > deploy/target.env
EOF
    exit 2
fi

KEY="${NEWS_ALERT_KEY:-$HOME/.ssh/news_alert_oracle}"
APP="${NEWS_ALERT_APP:-/opt/news-alert}"
SERVICE="${NEWS_ALERT_SERVICE:-news-alert.service}"
STAMP=$(date +%Y%m%d-%H%M%S)

SSH=(ssh -i "$KEY" -o ConnectTimeout=25 "$NEWS_ALERT_HOST")
remote() { "${SSH[@]}" "$@"; }

echo "== deploying to $NEWS_ALERT_HOST:$APP =="

echo "== 1. back up code and database =="
remote "sudo cp -a '$APP' '$APP.bak-$STAMP' \
        && cp '$APP/db/news_alert.sqlite3' ~/news_alert.sqlite3.bak-$STAMP \
        && echo 'backed up to $APP.bak-$STAMP and ~/news_alert.sqlite3.bak-$STAMP'"

echo "== 2. stop $SERVICE =="
remote "sudo systemctl stop '$SERVICE' && echo stopped"

echo "== 3. upload =="
remote "rm -rf /tmp/news-alert-deploy && mkdir -p /tmp/news-alert-deploy/news_alert /tmp/news-alert-deploy/scripts"
scp -i "$KEY" -o ConnectTimeout=25 -q news_alert/*.py "$NEWS_ALERT_HOST:/tmp/news-alert-deploy/news_alert/"
scp -i "$KEY" -o ConnectTimeout=25 -q scripts/*.py    "$NEWS_ALERT_HOST:/tmp/news-alert-deploy/scripts/"

echo "== 4. move into place and relabel for SELinux =="
remote "cp /tmp/news-alert-deploy/news_alert/*.py '$APP/news_alert/' \
        && cp /tmp/news-alert-deploy/scripts/*.py '$APP/scripts/' \
        && rm -rf /tmp/news-alert-deploy \
        && sudo restorecon -R '$APP/news_alert' '$APP/scripts' \
        && echo relabeled"

if [ "$RUN_MIGRATE" = 1 ]; then
    echo "== 5. migrate schema =="
    remote "cd '$APP' && PYTHONUNBUFFERED=1 '$APP/venv/bin/python' scripts/migrate_v2.py"
fi

if [ "$RUN_SEED_BIAS" = 1 ]; then
    echo "== 6. refresh bias data =="
    remote "cd '$APP' && PYTHONUNBUFFERED=1 '$APP/venv/bin/python' -c \"
import sys; sys.path.insert(0, '.')
from news_alert.bias import refresh_bias_data
from news_alert.db import db_cursor
with db_cursor() as cur:
    seeded, missing = refresh_bias_data(cur)
print('seeded', seeded, 'domains;', len(missing), 'AllSides names unmatched')
\""
fi

echo "== 7. restart =="
remote "sudo systemctl start '$SERVICE' && sleep 5 && systemctl is-active '$SERVICE'"

echo "== 8. verify =="
# The bracket in [b]ot_runner keeps this command's own remote shell -- whose command
# line contains the pattern -- from being counted as a second poller. Without it the
# check reports two pollers on a healthy single-poller host.
remote "sudo journalctl -u '$SERVICE' --since '-2min' --no-pager | tail -12; \
        echo '--- poller count (must be 1) ---'; \
        pgrep -fc '[b]ot_runner\.py' || echo 0"
