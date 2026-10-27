//! Read ZCode's local model telemetry without reading conversations or changing its database.
//! Terminal attempts are tracked in the durable ledger, not by insertion order: an older
//! running attempt can finish after a newer one. The ledger also survives detail rollups.
use crate::database::{lock_conn, Database};
use crate::error::AppError;
use crate::proxy::usage::{calculator::CostCalculator, parser::TokenUsage};
use crate::services::session_usage::SessionSyncResult;
use crate::services::sql_helpers::INPUT_TOKEN_SEMANTICS_FRESH;
use crate::services::usage_stats::find_model_pricing;
use rusqlite::{params, Connection, OpenFlags};
use rust_decimal::Decimal;
use std::collections::{HashMap, HashSet};
use std::time::Duration;

pub fn sync_zcode_usage(db: &Database) -> Result<SessionSyncResult, AppError> {
    let path = crate::config::get_home_dir().join(".zcode/cli/db/db.sqlite");
    if !path.exists() {
        return Ok(SessionSyncResult::default());
    }
    let source = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    source.busy_timeout(Duration::from_secs(1))?;
    sync_from_database(db, &source)
}

// ZCode's provider adapters can store either inclusive or exclusive input. Match its
// inputSideTokensFromStoredUsage rule, then store fresh input for CC Switch aggregation.
fn fresh_input(input: u32, output: u32, read: u32, write: u32, total: Option<i64>) -> u32 {
    let cached = u64::from(read) + u64::from(write);
    let inclusive = u64::from(input) + u64::from(output);
    let exclusive = inclusive + cached;
    if total.filter(|total| *total > 0).is_some_and(|total| {
        (total as u64).abs_diff(exclusive) < (total as u64).abs_diff(inclusive)
    }) {
        input
    } else {
        input.saturating_sub(read).saturating_sub(write)
    }
}

fn sync_from_database(db: &Database, source: &Connection) -> Result<SessionSyncResult, AppError> {
    // Older installations without model telemetry have nothing to import.
    if !source.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_usage')",
        [],
        |row| row.get::<_, bool>(0),
    )? {
        return Ok(SessionSyncResult::default());
    }
    let mut query = source.prepare(
        "SELECT id, session_id, model_id, status, started_at,
                duration_ms, time_to_first_token_ms, input_tokens, output_tokens,
                cache_read_input_tokens, cache_creation_input_tokens,
                COALESCE(provider_total_tokens, computed_total_tokens)
         FROM model_usage
         WHERE status IN ('completed', 'error', 'cancelled') AND completed_at IS NOT NULL
           AND (input_tokens > 0 OR output_tokens > 0 OR cache_read_input_tokens > 0
                OR cache_creation_input_tokens > 0)",
    )?;
    let mut rows = query.query([])?;
    let mut conn = lock_conn!(db.conn);
    let tx = conn.transaction()?;
    let seen = {
        let mut stmt = tx.prepare(
            "SELECT request_id FROM session_usage_dedup WHERE data_source='zcode_session'",
        )?;
        let ids = stmt
            .query_map([], |row| row.get::<_, String>(0))?
            .collect::<Result<HashSet<_>, _>>()?;
        ids
    };
    let mut pricing = HashMap::new();
    let mut result = SessionSyncResult {
        files_scanned: 1,
        ..Default::default()
    };
    while let Some(row) = rows.next()? {
        let id: String = row.get(0)?;
        let request_id = format!("zcode:{id}");
        if seen.contains(&request_id) {
            result.skipped += 1;
            continue;
        }
        let session: String = row.get(1)?;
        let model: String = row.get(2)?;
        let status: String = row.get(3)?;
        let started: i64 = row.get(4)?;
        let duration: Option<i64> = row.get(5)?;
        let ttft: Option<i64> = row.get(6)?;
        let input: u32 = row.get(7)?;
        let output: u32 = row.get(8)?;
        let read: u32 = row.get(9)?;
        let write: u32 = row.get(10)?;
        let usage = TokenUsage {
            input_tokens: fresh_input(input, output, read, write, row.get(11)?),
            output_tokens: output,
            cache_read_tokens: read,
            cache_creation_tokens: write,
            cache_creation_1h_tokens: 0,
            model: Some(model.clone()),
            message_id: None,
        };
        let price = pricing
            .entry(model.clone())
            .or_insert_with(|| find_model_pricing(&tx, &model));
        let cost = price
            .as_ref()
            .map(|price| CostCalculator::calculate(&usage, price, Decimal::ONE));
        let status_code = match status.as_str() {
            "completed" => 200,
            "cancelled" => 499,
            _ => 500,
        };
        let changed = tx.execute(
            "INSERT OR IGNORE INTO proxy_request_logs (
                request_id, provider_id, app_type, model, request_model,
                input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
                input_cost_usd, output_cost_usd, cache_read_cost_usd, cache_creation_cost_usd,
                total_cost_usd, latency_ms, first_token_ms, status_code, session_id, provider_type,
                is_streaming, cost_multiplier, created_at, data_source, input_token_semantics
             ) VALUES (?1, '_zcode_session', 'zcode', ?2, ?2, ?3, ?4, ?5, ?6,
                       ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, 'zcode_session',
                       1, '1', ?16, 'zcode_session', ?17)",
            params![
                request_id,
                model,
                usage.input_tokens,
                output,
                read,
                write,
                cost.as_ref()
                    .map_or(Decimal::ZERO, |c| c.input_cost)
                    .to_string(),
                cost.as_ref()
                    .map_or(Decimal::ZERO, |c| c.output_cost)
                    .to_string(),
                cost.as_ref()
                    .map_or(Decimal::ZERO, |c| c.cache_read_cost)
                    .to_string(),
                cost.as_ref()
                    .map_or(Decimal::ZERO, |c| c.cache_creation_cost)
                    .to_string(),
                cost.as_ref()
                    .map_or(Decimal::ZERO, |c| c.total_cost)
                    .to_string(),
                duration.unwrap_or(0).max(0),
                ttft.filter(|ms| *ms >= 0),
                status_code,
                session,
                started / 1000,
                INPUT_TOKEN_SEMANTICS_FRESH
            ],
        )?;
        tx.execute(
            "INSERT INTO session_usage_dedup (data_source, request_id, semantic_id, has_entry_id)
             VALUES ('zcode_session', ?1, ?1, 1)",
            [&request_id],
        )?;
        result.imported += changed as u32;
        result.skipped += u32::from(changed == 0);
    }
    tx.commit()?;
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn source() -> Connection {
        let source = Connection::open_in_memory().unwrap();
        source.execute_batch("CREATE TABLE model_usage (
            id TEXT PRIMARY KEY, session_id TEXT, model_id TEXT, status TEXT,
            started_at INTEGER, duration_ms INTEGER, time_to_first_token_ms INTEGER,
            input_tokens INTEGER, output_tokens INTEGER, cache_read_input_tokens INTEGER,
            cache_creation_input_tokens INTEGER, provider_total_tokens INTEGER,
            computed_total_tokens INTEGER, completed_at INTEGER, query_source TEXT);
            INSERT INTO model_usage VALUES
            ('older', 's', 'GLM-test', 'running', 100000, NULL, NULL, 0,0,0,0,NULL,0,NULL,'main_turn'),
            ('newer', 's', 'GLM-test', 'completed', 200000, 3000, 100, 1000,20,800,0,1020,1020,203000,'subagent'),
            ('title', 's', 'GLM-test', 'completed', 210000, 1000, NULL, 10,2,0,0,12,12,211000,'session_title'),
            ('failed', 's', 'GLM-test', 'error', 220000, 1000, NULL, 0,0,0,0,NULL,0,221000,'main_turn');").unwrap();
        source
    }

    #[test]
    fn normalizes_inclusive_and_exclusive_cache_without_double_counting() {
        assert_eq!(fresh_input(1000, 20, 800, 0, Some(1020)), 200);
        assert_eq!(fresh_input(200, 20, 700, 100, Some(1020)), 200);
        assert_eq!(fresh_input(1000, 20, 700, 100, None), 200);
        assert_eq!(fresh_input(0, 20, 800, 0, Some(820)), 0);
        assert_eq!(fresh_input(10, 20, 0, 0, Some(30)), 10);
    }

    #[test]
    fn imports_all_sources_and_late_completion_once_even_after_pruning() {
        let db = Database::memory().unwrap();
        let source = source();
        assert_eq!(sync_from_database(&db, &source).unwrap().imported, 2);
        assert_eq!(sync_from_database(&db, &source).unwrap().imported, 0);
        {
            let conn = db.conn.lock().unwrap();
            let totals: (u32,u32,u32) = conn.query_row(
                "SELECT SUM(input_tokens),SUM(output_tokens),SUM(cache_read_tokens) FROM proxy_request_logs",
                [], |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?))).unwrap();
            assert_eq!(totals, (210, 22, 800));
            conn.execute("DELETE FROM proxy_request_logs", []).unwrap();
        }
        source.execute("UPDATE model_usage SET status='completed', input_tokens=5, output_tokens=1, completed_at=300000 WHERE id='older'",[]).unwrap();
        assert_eq!(sync_from_database(&db, &source).unwrap().imported, 1);
        assert_eq!(sync_from_database(&db, &source).unwrap().imported, 0);
    }

    #[test]
    fn failure_rolls_back_ledger_and_rows_together() {
        let db = Database::memory().unwrap();
        let source = source();
        source
            .execute(
                "UPDATE model_usage SET input_tokens=-1 WHERE id='title'",
                [],
            )
            .unwrap();
        assert!(sync_from_database(&db, &source).is_err());
        let conn = db.conn.lock().unwrap();
        for table in ["proxy_request_logs", "session_usage_dedup"] {
            assert_eq!(
                conn.query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |r| r
                    .get::<_, u32>(0))
                    .unwrap(),
                0
            );
        }
    }

    #[test]
    fn old_database_without_telemetry_is_ignored() {
        let db = Database::memory().unwrap();
        assert_eq!(
            sync_from_database(&db, &Connection::open_in_memory().unwrap())
                .unwrap()
                .imported,
            0
        );
    }
}
