#!/bin/sh
# Runs inside the container before Home Assistant makes a backup (backup_pre):
# a consistent copy of the database goes into the backup, the live file is excluded.
cd /opt/ai_ha_control
exec python3 -c "from app.storage.db import write_backup; print(write_backup('${DATA_DIR:-/data}'))"
