-- ════════════════════════════════════════════════════════
-- GlobalBank Distributed System — Database Migration
-- Run this ONCE on each Supabase instance before starting
-- ════════════════════════════════════════════════════════

-- Add bank column to tx_requests for per-bank FIFO queuing
ALTER TABLE tx_requests
ADD COLUMN IF NOT EXISTS bank TEXT NOT NULL DEFAULT 'SBI';

-- Index for fast FIFO pickup per bank
CREATE INDEX IF NOT EXISTS idx_tx_requests_bank_status
ON tx_requests(bank, status, created_at);

-- Index for quick lock lookup
CREATE INDEX IF NOT EXISTS idx_account_locks_tx
ON account_locks(tx_id);

-- Index for transaction history
CREATE INDEX IF NOT EXISTS idx_transactions_sender
ON transactions(sender_account);

CREATE INDEX IF NOT EXISTS idx_transactions_receiver
ON transactions(receiver_account);

-- Full table definitions (if starting from scratch):
-- ────────────────────────────────────────────────────────

-- CREATE TABLE IF NOT EXISTS accounts (
--     account_id SERIAL PRIMARY KEY,
--     customer_name TEXT NOT NULL,
--     card_number CHAR(12) UNIQUE NOT NULL,
--     pin_hash TEXT NOT NULL,
--     balance NUMERIC NOT NULL DEFAULT 0 CHECK (balance >= 0),
--     status TEXT NOT NULL DEFAULT 'ACTIVE'
-- );

-- CREATE TABLE IF NOT EXISTS account_locks (
--     tx_id TEXT PRIMARY KEY,
--     account_id TEXT NOT NULL,
--     amount NUMERIC NOT NULL,
--     lock_time TIMESTAMP DEFAULT NOW()
-- );

-- CREATE TABLE IF NOT EXISTS transactions (
--     tx_id TEXT PRIMARY KEY,
--     tx_type TEXT NOT NULL CHECK (tx_type IN ('TRANSFER', 'DEPOSIT', 'WITHDRAW')),
--     sender_account TEXT,
--     receiver_account TEXT,
--     amount NUMERIC NOT NULL,
--     role TEXT CHECK (role IN ('SENDER', 'RECEIVER')),
--     state TEXT NOT NULL CHECK (state IN ('INIT', 'PREPARED', 'COMMITTED', 'ABORTED')),
--     created_at TIMESTAMP DEFAULT NOW()
-- );

-- CREATE TABLE IF NOT EXISTS tx_requests (
--     request_id SERIAL PRIMARY KEY,
--     tx_id TEXT UNIQUE NOT NULL,
--     account_id TEXT NOT NULL,
--     tx_type TEXT NOT NULL CHECK (tx_type IN ('TRANSFER', 'DEPOSIT', 'WITHDRAW')),
--     amount NUMERIC NOT NULL CHECK (amount > 0),
--     status TEXT NOT NULL DEFAULT 'PENDING'
--         CHECK (status IN ('PENDING', 'PROCESSING', 'DONE', 'FAILED')),
--     bank TEXT NOT NULL DEFAULT 'SBI',
--     created_at TIMESTAMP DEFAULT NOW()
-- );

-- CREATE TABLE IF NOT EXISTS audit_log (
--     id SERIAL PRIMARY KEY,
--     tx_id TEXT,
--     message TEXT,
--     timestamp TIMESTAMP DEFAULT NOW()
-- );
