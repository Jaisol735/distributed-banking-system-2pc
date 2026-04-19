# GlobalBank — Distributed Inter-Bank Transaction Simulator

A from-scratch simulation of real-world inter-bank money transfer infrastructure, built to demonstrate **distributed systems concepts** in a banking context: Two-Phase Commit (2PC), FIFO queuing, crash recovery, hierarchical server architecture, and multi-database consistency.

This project demonstrates how real banking systems maintain consistency across distributed services without data loss during failures.

> **What this is:** A teaching/demo project. Not production banking software. All data is test data.

---

## Why This Exists

Modern inter-bank transfers (NEFT, SWIFT, ACH) rely on distributed systems guarantees that are rarely taught in college courses. This project implements those core ideas from scratch:

- How do you ensure money doesn't disappear or duplicate when a server crashes mid-transfer?
- How do you coordinate two banks that each own their own database?
- How do you handle concurrent deposits/withdrawals on the same account without corruption?

---

## Architecture

```
Browser (ATM UI)
       │
       ▼
Frontend Flask  (localhost:8080)   ← proxy layer
       │
       ▼
Bank Servers: SBI · HDFC · JPMC · BOA   ← one process per bank, own Supabase DB
       │
       ▼
Country Servers: India · USA   ← routing + cross-bank coordination
       │
       ▼
World Server (port 4000)   ← health monitoring, cross-country validation
```

Each layer is a separate Flask process. Banks talk up the chain only when necessary (cross-bank or cross-country transfers). Same-bank transfers never leave the bank server.

---

## Key Technical Concepts Implemented

### Two-Phase Commit (2PC)
Every inter-bank transfer runs a full 2PC protocol:

1. **INIT** — Sender bank validates sender balance and locks funds
2. **PREPARE** — Receiver bank checks if account exists and is ready
3. **COMMIT** — Both sides finalize; money moves atomically
4. **ABORT** — If any step fails, sender-side lock is released; no partial state

Cross-bank endpoints per bank: `/receive_prepare`, `/receive_commit`, `/receive_abort`

### FIFO Transaction Queue (Deadlock-Free)
Deposits and withdrawals are queued, not executed inline. A background worker processes them using:
```sql
SELECT ... FOR UPDATE SKIP LOCKED
```
This prevents deadlocks when multiple requests hit the same account simultaneously.

### Crash Recovery
Every server layer has a paired recovery script (`*_recovery.py`). On startup, each server re-reads its log and replays any incomplete transactions. If a bank crashes mid-2PC, the recovery script determines whether to commit or abort based on logged state.

### Health Monitoring
The World Server runs a background thread that pings all country servers every N seconds. If a destination country or bank is offline, a transfer is rejected immediately rather than hanging.

### Hierarchical Routing
- **Same bank** → handled entirely at bank level
- **Cross-bank, same country** → country server coordinates
- **Cross-country** → world server validates destination, then country servers manage 2PC

---

## Tech Stack

| Layer | Tech |
|---|---|
| All servers | Python, Flask |
| Database | Supabase (PostgreSQL) — one instance per bank |
| Networking | Tailscale (cross-machine) or localhost (same machine) |
| Frontend | Vanilla HTML/CSS/JS (ATM-style UI) |
| Config | JSON-based server registry |

---

## Project Structure

```
GlobalBank/
├── Server/
│   ├── world_server/
│   │   ├── world.py              # World server: health monitor, cross-country routing
│   │   └── world_recovery.py
│   ├── India/
│   │   ├── india.py              # Country server: routes India transfers
│   │   ├── india_recovery.py
│   │   ├── HDFC/
│   │   │   ├── hdfc.py           # Bank server: full 2PC + FIFO + Supabase
│   │   │   └── hdfc_recovery.py
│   │   └── SBI/
│   │       ├── sbi.py
│   │       └── sbi_recovery.py
│   ├── USA/
│   │   ├── usa.py
│   │   ├── BOA/
│   │   └── JPMC/
│   ├── config.json               # Server port + IP registry
│   └── config_loader.py          # Tailscale/localhost URL resolver
├── frontend/
│   ├── app.py                    # Flask proxy + UI server
│   └── templates/index.html      # ATM-style browser UI
├── migration.sql                  # Supabase schema setup
└── project_structure.html         # Architecture diagram
```

---

## Running Locally (All Servers on One Machine)

### 1. Database setup
Run `migration.sql` in each bank's Supabase project SQL editor.  
You need 4 separate Supabase projects: `HDFC`, `SBI`, `JPMC`, `BOA`.

### 2. Configure `Server/config.json`
```json
{
  "tailscale_ip": "localhost",
  "ports": {
    "world": 4000,
    "india": 4001,
    "usa": 4002,
    "hdfc": 4010,
    "sbi": 4011,
    "boa": 4020,
    "jpmc": 4021,
    "frontend": 8080
  }
}
```

### 3. Install dependencies
```bash
pip install flask requests supabase python-dotenv
```

### 4. Start all servers (8 separate terminals or use a process manager)
```bash
# World
python Server/world_server/world.py

# Country servers
python Server/India/india.py
python Server/USA/usa.py

# Bank servers
python Server/India/HDFC/hdfc.py
python Server/India/SBI/sbi.py
python Server/USA/BOA/boa.py
python Server/USA/JPMC/jpmc.py

# Frontend
python frontend/app.py
```

Open: `http://localhost:8080`

### Running Across Two Machines
Set `tailscale_ip` in `config.json` to your Tailscale IP. Run bank + country + world servers on PC-2, frontend on PC-1.

---

## What You Can Test

| Operation | Behavior |
|---|---|
| Signup / Login | Creates account + card number per bank |
| Deposit / Withdraw | Queued via FIFO, processed atomically |
| Same-bank transfer | Handled locally, no inter-server calls |
| Cross-bank transfer (India) | Full 2PC between two bank servers |
| Cross-country transfer | World → Country → Bank 2PC chain |
| Server crash mid-transfer | Run recovery script; state is restored |

---

## Honest Limitations

- **Synthetic test data only** — no real bank connections
- **No TLS** — HTTP between servers; fine for local/demo, not production
- **Single-region Supabase** — real banking uses geo-distributed replicas
- **XA-style 2PC is blocking** — production systems use sagas or async compensation
- Transfer UI is minimal (ATM screen); not a full banking app

---

## Concepts Demonstrated

`Distributed Systems` · `Two-Phase Commit` · `FIFO Queuing` · `Crash Recovery` · `Hierarchical Architecture` · `Flask` · `PostgreSQL` · `Supabase` · `Concurrent Transaction Processing`
