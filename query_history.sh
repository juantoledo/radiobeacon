#!/usr/bin/env bash
# Useful queries against storage/radiobeacon.db's items table, centered on
# event_key (the field that groups an event's separate update snapshots
# into one timeline — see adapters/src/adapters/storage.py).
set -euo pipefail
cd "$(dirname "$0")"

DB="storage/radiobeacon.db"

# Pull --db <path> out of the args wherever it appears, leaving the rest
# (command + its own args) in order.
args=()
while [ $# -gt 0 ]; do
  case "$1" in
    --db)
      DB="${2:?--db requires a path}"
      shift 2
      ;;
    *)
      args+=("$1")
      shift
      ;;
  esac
done
set -- "${args[@]+"${args[@]}"}"

CMD="${1:-}"

usage() {
  cat <<EOF
Usage: $0 <command> [args]

Commands:
  history <event_key>     Full chronological history of one event
  multi [limit]           Events with more than one update, most active first (default limit: 20)
  latest [limit]          Current state — latest item per event (default limit: 20)
  duration [limit]        Update count + total duration per event, longest first (default limit: 20)
  gaps <event_key>        Time gap (hours) between consecutive updates within one event
  escalated <from> <to>   Events whose titles mention both patterns (e.g. escalated Amarilla -> "Alerta Roja")
  events [pattern]        List distinct event_keys, optionally filtered by title LIKE pattern

  --db <path>             Use a different database file (default: storage/radiobeacon.db)

Examples:
  $0 multi
  $0 history se-declara-alerta-amarilla-...-2026-08-16-21-09-09
  $0 escalated Amarilla "Alerta Roja"
EOF
}

sql() {
  sqlite3 -header -column "$DB" "$1"
}

case "$CMD" in
  history)
    event_key="${2:?Usage: $0 history <event_key>}"
    sqlite3 -header -column "$DB" \
      "SELECT item_id, source_date_time, extracted_title, url FROM items WHERE event_key = '${event_key//\'/\'\'}' ORDER BY source_date_time;"
    ;;

  multi)
    limit="${2:-20}"
    sql "SELECT event_key, COUNT(*) AS updates,
                MIN(source_date_time) AS first_seen,
                MAX(source_date_time) AS last_seen,
                MAX(url) AS url,
                (SELECT item_id FROM items i2 WHERE i2.event_key = items.event_key
                 ORDER BY source_date_time DESC LIMIT 1) AS latest_item_id
         FROM items
         WHERE event_key IS NOT NULL
         GROUP BY event_key
         HAVING COUNT(*) > 1
         ORDER BY updates DESC, last_seen DESC
         LIMIT $limit;"
    ;;

  latest)
    limit="${2:-20}"
    sql "SELECT i.event_key, i.item_id, i.source_date_time, i.extracted_title, i.url
         FROM items i
         JOIN (
             SELECT event_key, MAX(source_date_time) AS latest
             FROM items
             WHERE event_key IS NOT NULL
             GROUP BY event_key
         ) latest_per_event
           ON i.event_key = latest_per_event.event_key
          AND i.source_date_time = latest_per_event.latest
         ORDER BY i.source_date_time DESC
         LIMIT $limit;"
    ;;

  duration)
    limit="${2:-20}"
    sql "SELECT event_key, COUNT(*) AS updates,
                ROUND((julianday(MAX(source_date_time)) - julianday(MIN(source_date_time))) * 24, 1) AS duration_hours,
                MAX(source_date_time) AS last_seen,
                MAX(url) AS url,
                (SELECT item_id FROM items i2 WHERE i2.event_key = items.event_key
                 ORDER BY source_date_time DESC LIMIT 1) AS latest_item_id
         FROM items
         WHERE event_key IS NOT NULL
         GROUP BY event_key
         ORDER BY duration_hours DESC, last_seen DESC
         LIMIT $limit;"
    ;;

  gaps)
    event_key="${2:?Usage: $0 gaps <event_key>}"
    sqlite3 -header -column "$DB" \
      "SELECT item_id, source_date_time, extracted_title, url,
              ROUND((julianday(source_date_time) - julianday(LAG(source_date_time) OVER (ORDER BY source_date_time))) * 24, 2) AS hours_since_prev
       FROM items
       WHERE event_key = '${event_key//\'/\'\'}'
       ORDER BY source_date_time;"
    ;;

  escalated)
    from="${2:?Usage: $0 escalated <from_pattern> <to_pattern>}"
    to="${3:?Usage: $0 escalated <from_pattern> <to_pattern>}"
    from_escaped="${from//\'/\'\'}"
    to_escaped="${to//\'/\'\'}"
    sqlite3 -header -column "$DB" \
      "SELECT event_key, MAX(source_date_time) AS last_seen, MAX(url) AS url,
              (SELECT item_id FROM items i2 WHERE i2.event_key = items.event_key
               ORDER BY source_date_time DESC LIMIT 1) AS latest_item_id
       FROM items
       WHERE event_key IS NOT NULL
         AND extracted_title LIKE '%${to_escaped}%'
         AND event_key IN (SELECT event_key FROM items WHERE extracted_title LIKE '%${from_escaped}%')
       GROUP BY event_key
       ORDER BY last_seen DESC;"
    ;;

  events)
    pattern="${2:-}"
    if [ -n "$pattern" ]; then
      pattern_escaped="${pattern//\'/\'\'}"
      sql "SELECT event_key, MIN(extracted_title) AS sample_title, MAX(source_date_time) AS last_seen, MAX(url) AS url,
                  (SELECT item_id FROM items i2 WHERE i2.event_key = items.event_key
                   ORDER BY source_date_time DESC LIMIT 1) AS latest_item_id
           FROM items
           WHERE event_key IS NOT NULL AND extracted_title LIKE '%${pattern_escaped}%'
           GROUP BY event_key
           ORDER BY last_seen DESC;"
    else
      sql "SELECT event_key, COUNT(*) AS updates, MAX(source_date_time) AS last_seen, MAX(url) AS url,
                  (SELECT item_id FROM items i2 WHERE i2.event_key = items.event_key
                   ORDER BY source_date_time DESC LIMIT 1) AS latest_item_id
           FROM items
           WHERE event_key IS NOT NULL
           GROUP BY event_key
           ORDER BY last_seen DESC;"
    fi
    ;;

  -h|--help|"")
    usage
    ;;

  *)
    echo "Unknown command: $CMD" >&2
    usage >&2
    exit 1
    ;;
esac
