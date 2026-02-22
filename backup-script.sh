#!/bin/sh

# PostgreSQL Backup Script - Simple, working version
# Uses environment variables passed from docker-compose
# Backs up all databases

set -e

BACKUP_BASE_DIR="/backups"
DB_USER="${POSTGRES_USER}"
DB_HOST="${POSTGRES_HOST}"
DB_PASSWORD="${POSTGRES_PASSWORD}"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
RCLONE_REMOTE="${RCLONE_REMOTE:-gdrive}"
RCLONE_PATH="${RCLONE_PATH:-postgres-backups}"
LOCAL_RETENTION_DAYS=7
GOOGLE_DRIVE_RETENTION_DAYS=30

mkdir -p "$BACKUP_BASE_DIR"

echo "[$(date)] =========================================="
echo "[$(date)] Starting PostgreSQL backup"
echo "[$(date)] Host: $DB_HOST"
echo "[$(date)] User: $DB_USER"
echo "[$(date)] Timestamp: $TIMESTAMP"
echo "[$(date)] =========================================="

# Export password for psql/pg_dump to use
export PGPASSWORD="$DB_PASSWORD"

# Get database list
echo "[$(date)] Fetching database list..."
DATABASES=$(psql -h "$DB_HOST" -U "$DB_USER" -d n8n_db -t -A -c "
  SELECT datname FROM pg_database 
  WHERE datistemplate = false 
  AND datname NOT IN ('postgres', 'template0', 'template1')
  ORDER BY datname;")

if [ $? -ne 0 ]; then
    echo "[$(date)] ✗ ERROR: Could not fetch database list" >&2
    echo "[$(date)] Check that POSTGRES_PASSWORD is correct" >&2
    exit 1
fi

DB_COUNT=$(echo "$DATABASES" | wc -l)
echo "[$(date)] ✓ Found $DB_COUNT databases"
echo "[$(date)] Databases: $(echo $DATABASES | tr '\n' ' ')"
echo "[$(date)] =========================================="

FAILED_DBS=""
SUCCESSFUL_COUNT=0

# Backup each database
for DB in $DATABASES; do
    # Skip empty lines
    DB=$(echo "$DB" | xargs)
    [ -z "$DB" ] && continue
    
    # Create backup directory for this database
    DB_BACKUP_DIR="$BACKUP_BASE_DIR/$DB"
    mkdir -p "$DB_BACKUP_DIR"
    BACKUP_FILE="$DB_BACKUP_DIR/${DB}_${TIMESTAMP}.sql.gz"
    
    echo "[$(date)] Backing up: $DB"
    
    # Use pg_dump to backup
    if pg_dump -h "$DB_HOST" -U "$DB_USER" "$DB" 2>/dev/null | gzip -9 > "$BACKUP_FILE"; then
        FILE_SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
        echo "[$(date)]   ✓ Created $BACKUP_FILE ($FILE_SIZE)"
        SUCCESSFUL_COUNT=$((SUCCESSFUL_COUNT + 1))
    else
        echo "[$(date)]   ✗ Failed to backup $DB" >&2
        FAILED_DBS="$FAILED_DBS $DB"
    fi
done

echo "[$(date)] =========================================="
echo "[$(date)] Backup Results: $SUCCESSFUL_COUNT/$DB_COUNT successful"

# Upload to Google Drive if rclone available
if command -v rclone &> /dev/null; then
    echo "[$(date)] Uploading to Google Drive..."
    
    for DB in $DATABASES; do
        DB=$(echo "$DB" | xargs)
        [ -z "$DB" ] && continue
        DB_BACKUP_DIR="$BACKUP_BASE_DIR/$DB"
        
        if [ -d "$DB_BACKUP_DIR" ]; then
            if rclone copy "$DB_BACKUP_DIR" "gdrive:$RCLONE_PATH/$DB/" --progress 2>/dev/null; then
                echo "[$(date)]   ✓ Uploaded $DB to Google Drive"
            else
                echo "[$(date)]   ✗ Failed to upload $DB" >&2
            fi
        fi
    done
    
    # Cleanup old local backups
    echo "[$(date)] Cleaning up local backups older than $LOCAL_RETENTION_DAYS days..."
    for DB in $DATABASES; do
        DB=$(echo "$DB" | xargs)
        [ -z "$DB" ] && continue
        DB_BACKUP_DIR="$BACKUP_BASE_DIR/$DB"
        
        if [ -d "$DB_BACKUP_DIR" ]; then
            DELETED=$(find "$DB_BACKUP_DIR" -name "${DB}_*.sql.gz" -mtime +$LOCAL_RETENTION_DAYS -delete -print 2>/dev/null | wc -l)
            [ "$DELETED" -gt 0 ] && echo "[$(date)]   ✓ Deleted $DELETED old backup(s) from $DB"
        fi
    done
    
    # Cleanup old Google Drive backups
    echo "[$(date)] Cleaning up Google Drive backups older than $GOOGLE_DRIVE_RETENTION_DAYS days..."
    for DB in $DATABASES; do
        DB=$(echo "$DB" | xargs)
        [ -z "$DB" ] && continue
        rclone delete "gdrive:$RCLONE_PATH/$DB/" --min-age ${GOOGLE_DRIVE_RETENTION_DAYS}d 2>/dev/null && echo "[$(date)]   ✓ Cleaned Google Drive for $DB" || true
    done
fi

echo "[$(date)] =========================================="
if [ -n "$FAILED_DBS" ]; then
    echo "[$(date)] ✗ Some databases failed:$FAILED_DBS" >&2
    exit 1
else
    echo "[$(date)] ✓ Backup completed successfully"
    exit 0
fi
