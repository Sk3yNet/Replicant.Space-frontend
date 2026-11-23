# To do

Ideas and requests not built yet, newest first.

## Split the database into history and live state (Joe 2026-11-23)

Move history (events, actions, notifications: most of the 1.3 GB) into its own SQLite file, keeping live state (kv,
kv_rows, device_rows, timers, systems) in the main one. Gains: small live file (fast warm-up, checkpoints, VACUUM),
own write-ahead logs and connections, so history scans never hold up live reads. Costs: one-time move of the history
tables at startup, rewiring queries that join the two, snapshots/backups cover two files. Doesn't cut write volume.
Decide after a day of Diagnostics › Performance "Saved values written" (1.534.0): build it if events and the command
log dominate what's left.
