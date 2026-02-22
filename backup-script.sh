#!/bin/sh

# PostgreSQL Backup Script - Backs up ALL databases
# Automatically discovers all databases, creates organized backups
# Uploads to Google Drive via rclone
# Auto-cleans old backups per database

set -e

BACKUP_BASE_DIR="/backups"
DB_USER="${POSTGRES_USER:-n8n_user}"
DB_HOST="${POSTGRES_HOST:-postgres}"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
RCLONE_REMOTE="${RCLONE_REMOTE:-gdrive}"
RCLONE_PATH="${RCLONE_PATH:-postgres-backups}"
LOCAL_RETENTION_DAYS=7
GOOGLE_DRIVE_RETENTION_DAYS=30

# Create base backup directory if it doesn't exist
mkdir -p "$BACKUP_BASE_DIR"

echo "[$(date)] =========================================="
echo "[$(date)] Starting comprehensive PostgreSQL backup"
echo "[$(date)] Backup timestamp: $TIMESTAMP"
echo "[$(date)] =========================================="

# Get list of all databases (excluding system databases)
DATABASES=$(PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$DB_HOST" -U "$DB_USER" -t -c "
  SELECT datname FROM pg_database 
  WHERE datistemplate = false 
  AND datname NOT IN ('postgres', 'template0', 'template1')
  ORDER BY datname;
")

if [ -z "$DATABASES" ]; then
    echo "[$(date)] ✗ ERROR: Could not fetch database list" >&2
    exit 1
fi

DB_COUNT=$(echo "$DATABASES" | wc -l)
echo "[$(date)] Found $DB_COUNT database(s) to backup"
echo "[$(date)] =========================================="

FAILED_DBS=""
SUCCESSFUL_COUNT=0
TOTAL_SIZE=0

# Backup each database
for DB in $DATABASES; do
    # Skip empty lines
    [ -z "$DB" ] && continue
    
    # Create database-specific backup directory
    DB_BACKUP_DIR="$BACKUP_BASE_DIR/$DB"
    mkdir -p "$DB_BACKUP_DIR"
    
    BACKUP_FILE="$DB_BACKUP_DIR/${DB}_${TIMESTAMP}.sql.gz"
    
    echo "[$(date)] Backing up database: $DB"
    
    if PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -h "$DB_HOST" -U "$DB_USER" "$DB" | gzip -9 > "$BACKUP_FILE" 2>/dev/null; then
        FILE_SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
        FILE_SIZE_BYTES=$(stat -f%z "$BACKUP_FILE" 2>/dev/null || stat -c%s "$BACKUP_FILE" 2>/dev/null || echo "0")
        TOTAL_SIZE=$((TOTAL_SIZE + FILE_SIZE_BYTES))
        
        echo "[$(date)]   ✓ Backup created: $BACKUP_FILE ($FILE_SIZE)"
        SUCCESSFUL_COUNT=$((SUCCESSFUL_COUNT + 1))
    else
        echo "[$(date)]   ✗ ERROR: Failed to backup $DB" >&2
        FAILED_DBS="$FAILED_DBS $DB"
    fi
done

echo "[$(date)] =========================================="
echo "[$(date)] Backup phase complete"
echo "[$(date)] Successful: $SUCCESSFUL_COUNT/$DB_COUNT"
if [ -n "$FAILED_DBS" ]; then
    echo "[$(date)] Failed:$FAILED_DBS"
fi
echo "[$(date)] Total backup size: $(numfmt --to=iec-i --suffix=B $TOTAL_SIZE 2>/dev/null || echo '$((TOTAL_SIZE/1024/1024))MB')"
echo "[$(date)] =========================================="

# Upload to Google Drive if rclone is available
if command -v rclone &> /dev/null; then
    echo "[$(date)] Uploading backups to Google Drive..."
    
    UPLOAD_SUCCESS=0
    UPLOAD_FAILED=0
    
    for DB in $DATABASES; do
        [ -z "$DB" ] && continue
        DB_BACKUP_DIR="$BACKUP_BASE_DIR/$DB"
        
        if [ -d "$DB_BACKUP_DIR" ]; then
            RCLONE_DB_PATH="$RCLONE_PATH/$DB"
            
            if rclone copy "$DB_BACKUP_DIR" "gdrive:$RCLONE_DB_PATH/" --progress 2>/dev/null; then
                echo "[$(date)]   ✓ Uploaded $DB backups to Google Drive"
                UPLOAD_SUCCESS=$((UPLOAD_SUCCESS + 1))
            else
                echo "[$(date)]   ✗ Failed to upload $DB to Google Drive" >&2
                UPLOAD_FAILED=$((UPLOAD_FAILED + 1))
            fi
        fi
    done
    
    echo "[$(date)] =========================================="
    echo "[$(date)] Upload phase complete"
    echo "[$(date)] Successful uploads: $UPLOAD_SUCCESS"
    if [ "$UPLOAD_FAILED" -gt 0 ]; then
        echo "[$(date)] Failed uploads: $UPLOAD_FAILED"
    fi
    echo "[$(date)] =========================================="
    
    # Clean up old local backups per database
    echo "[$(date)] Cleaning up local backups older than $LOCAL_RETENTION_DAYS days..."
    DELETED_COUNT=0
    
    for DB in $DATABASES; do
        [ -z "$DB" ] && continue
        DB_BACKUP_DIR="$BACKUP_BASE_DIR/$DB"
        
        if [ -d "$DB_BACKUP_DIR" ]; then
            DELETED=$(find "$DB_BACKUP_DIR" -name "${DB}_*.sql.gz" -mtime +$LOCAL_RETENTION_DAYS -delete -print | wc -l)
            if [ "$DELETED" -gt 0 ]; then
                echo "[$(date)]   ✓ Deleted $DELETED old backup(s) from $DB"
                DELETED_COUNT=$((DELETED_COUNT + DELETED))
            fi
        fi
    done
    
    if [ "$DELETED_COUNT" -gt 0 ]; then
        echo "[$(date)] Total local backups deleted: $DELETED_COUNT"
    else
        echo "[$(date)] No local backups older than $LOCAL_RETENTION_DAYS days to delete"
    fi
    
    # Clean up old backups on Google Drive per database
    echo "[$(date)] Cleaning up Google Drive backups older than $GOOGLE_DRIVE_RETENTION_DAYS days..."
    GDRIVE_CLEANED=0
    
    for DB in $DATABASES; do
        [ -z "$DB" ] && continue
        RCLONE_DB_PATH="$RCLONE_PATH/$DB"
        
        if rclone delete "gdrive:$RCLONE_DB_PATH/" --min-age ${GOOGLE_DRIVE_RETENTION_DAYS}d --exclude "*.lock" 2>/dev/null; then
            echo "[$(date)]   ✓ Cleaned up old Google Drive backups for $DB"
            GDRIVE_CLEANED=$((GDRIVE_CLEANED + 1))
        fi
    done
    
    if [ "$GDRIVE_CLEANED" -gt 0 ]; then
        echo "[$(date)] Google Drive cleanup complete for $GDRIVE_CLEANED database(s)"
    fi
    
    echo "[$(date)] =========================================="
    echo "[$(date)] ✓ Backup and cleanup completed successfully"
    echo "[$(date)] =========================================="
else
    echo "[$(date)] ✗ WARNING: rclone not found in backup container"
    echo "[$(date)] Install rclone to enable Google Drive uploads"
    echo "[$(date)] Local backups are available at: $BACKUP_BASE_DIR"
    echo "[$(date)] =========================================="
fi

# Exit with error if any backups failed
if [ -n "$FAILED_DBS" ]; then
    echo "[$(date)] ERROR: Some database backups failed" >&2
    exit 1
fi

exit 0
