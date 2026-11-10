# Backup and restore

PostgreSQL is the system of record for tasks, grants, approvals, memory, goals, and connections. Media files are on the API host and are not inside the database dump. A dump does not include WAL segments that were not part of that dump, and restoring a dump does not erase older backups.

## What to copy

1. Take a custom-format dump from the running database:

```bash
pg_dump -U apex -d apex -Fc -f apex.dump
```

2. Copy `apex.dump` to storage the database host cannot rewrite. Encrypt that copy with the operator's key. This repository does not store that key.
3. Record the Alembic revision from `alembic_version`. A dump restores that revision. Run `alembic upgrade head` only when the application you start is newer than the dump.

The application does not run `pg_dump` itself. The operator does.

## Restore proof

`python -m tests.integration.backup_restore` starts PostgreSQL 16 as container `apex-m8-backup` on `127.0.0.1:55434` with volume `apex_m8_backup`. It upgrades an empty database, writes a marker into `runtime_controls`, dumps it, drops `public`, restores the dump, and checks the marker and revision `0009_controls`. It then removes that container and volume. It does not attach `docker_apex_pg_data`.

A successful run prints `backup-restore-ok`. That proves this dump format can be restored into an empty database. It does not prove a particular production backup was taken.

## Restore steps

1. Stop the API and the worker so they do not write during the restore.
2. Restore into an empty database, or into a new volume. Do not restore over the only remaining copy of the dump.
3. Confirm `alembic_version` and one known row, such as a principal or `runtime_controls`.
4. Start `migrate` only if the selected application revision is newer than the dump.
5. Start one API and one worker. `GET /ready` is 200 only when authentication is configured and the schema matches.
6. Read one task that existed before the dump. Confirm an uncertain execution is still uncertain.

## What restore does not do

It does not delete WAL from the old volume. It does not delete provider logs or a previous dump. It does not restore temporary media files that were never in PostgreSQL. Memory purge rules in `docs/security/data-lifecycle.md` still apply to the restored rows.
