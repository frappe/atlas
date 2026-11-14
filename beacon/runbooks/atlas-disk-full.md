# Atlas disk almost full

A file system of the Atlas server has less than 10% free space. MariaDB and the bench stop when it is full.

1. Run `df -h` and `du -xh --max-depth=2 <path> | sort -h | tail` on the Atlas server.
2. Delete old backups and logs in `sites/*/private/backups` and `logs/`.
3. Run `bench --site <site> trim-tables` for large log tables.
4. If the data is real growth, resize the disk.
