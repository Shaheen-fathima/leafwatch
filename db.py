import sqlite3, json, time, math, os
from contextlib import contextmanager
from pathlib import Path
import numpy as np

# LEAFWATCH_DB lets tests (or you) point at another file; the default is the same leafwatch.db next to this file.
DB_PATH = Path(os.getenv("LEAFWATCH_DB") or Path(__file__).parent / "leafwatch.db")
SCHEMA_VERSION = 3          # PRAGMA user_version. 0/1 = original, 2 = v2 (quality/duplicates/approval), 3 = v3 (request answers, reminders). Additive only
SCHEMA = """
CREATE TABLE IF NOT EXISTS farmers(id INTEGER PRIMARY KEY, name TEXT, lat REAL, lon REAL, a REAL DEFAULT 3, b REAL DEFAULT 2, points INTEGER DEFAULT 0, created REAL);
CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY, farmer_id INTEGER, lat REAL, lon REAL, ts REAL, crop TEXT, top_class TEXT, top_p REAL,
  u REAL, probs TEXT, emb BLOB, dhash TEXT, w REAL, dep_score REAL, cluster_id INTEGER, image TEXT, action TEXT, post_p REAL);
CREATE INDEX IF NOT EXISTS ix_rep_ts ON reports(ts);
CREATE TABLE IF NOT EXISTS outbreaks(id INTEGER PRIMARY KEY, crop TEXT, disease TEXT, lat REAL, lon REAL, ts REAL, P REAL, n_eff REAL,
  status TEXT DEFAULT 'active', evidence TEXT, alerted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY, farmer_id INTEGER, outbreak_id INTEGER, msg_en TEXT, msg_ta TEXT, dist_km REAL, ts REAL, read INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_alert_f ON alerts(farmer_id);
CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY, farmer_id INTEGER, crop TEXT, disease TEXT, lat REAL, lon REAL, ts REAL, status TEXT DEFAULT 'pending');
CREATE INDEX IF NOT EXISTS ix_req_f ON requests(farmer_id);
"""


@contextmanager
def tx(path=None):
    c = sqlite3.connect(str(path or DB_PATH), timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback(); raise
    finally:
        c.close()


# Columns added after the first release. Only ADD COLUMN is ever used: existing rows and columns are never touched.
NEW_COLUMNS = {
    "reports": [("n_eff", "REAL"), ("n_src", "INTEGER"), ("conflict", "REAL"), ("costs", "TEXT"), ("repeat_kind", "TEXT"),
                ("verify_status", "TEXT"), ("note", "TEXT"), ("pts", "INTEGER")],
    "requests": [("report_id", "INTEGER"), ("answered_report_id", "INTEGER"), ("answer", "TEXT"), ("answered_ts", "REAL"), ("reminded_ts", "REAL")],
    "outbreaks": [("reviewed_ts", "REAL")],
}


def backup(path, tag):
    """Consistent copy of the live database (works with WAL) into <db folder>/backups/. Never overwrites an existing backup."""
    path = Path(path)
    dst_dir = path.parent / "backups"; dst_dir.mkdir(exist_ok=True)
    dst = dst_dir / f"{path.name}.{tag}-{time.strftime('%Y%m%d-%H%M%S')}.bak"
    n = 1
    while dst.exists():
        dst = dst_dir / f"{path.name}.{tag}-{time.strftime('%Y%m%d-%H%M%S')}-{n}.bak"; n += 1
    src = sqlite3.connect(str(path)); out = sqlite3.connect(str(dst))
    try:
        src.backup(out)
    finally:
        out.close(); src.close()
    return dst


def migrate(c):
    """Idempotent, data-preserving upgrade. Safe to run on every start-up."""
    for table, cols in NEW_COLUMNS.items():
        have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        for name, typ in cols:
            if name not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
    c.execute("CREATE INDEX IF NOT EXISTS ix_req_rep ON requests(report_id)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_alert_o ON alerts(outbreak_id)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_rep_verify ON reports(verify_status)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_out_status ON outbreaks(status)")
    try:                                           # one alert per farmer per outbreak, enforced by the database itself
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_alert_once ON alerts(farmer_id, outbreak_id)")
    except sqlite3.IntegrityError:
        print("!! alerts table already contains duplicate (farmer, outbreak) rows; unique index skipped (application logic still prevents new duplicates)")


def init(path=None):
    p = Path(path or DB_PATH)
    existed = p.exists() and p.stat().st_size > 0
    if existed:                                    # back up BEFORE the first upgrade of an existing database
        probe = sqlite3.connect(str(p))
        try:
            ver = probe.execute("PRAGMA user_version").fetchone()[0]
        finally:
            probe.close()
        if ver < SCHEMA_VERSION:
            print("LeafWatch: existing database found, backup written to", backup(p, f"pre-v{SCHEMA_VERSION}"))
    with tx(path) as c:
        c.executescript(SCHEMA)
        migrate(c)
        c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def report_row(r):
    d = dict(r)
    d["probs"] = np.array(json.loads(d["probs"]), dtype=np.float64)
    e = np.frombuffer(d["emb"], dtype=np.float32).astype(np.float64)
    d["emb"] = e / (np.linalg.norm(e) + 1e-9)
    d["dhash"] = int(d["dhash"], 16)
    if d.get("w") is None:                       # rows written by very old versions
        d["w"] = 1.0
    return d


def recent_reports(c, lat, lon, since, km=12.0):
    dlat, dlon = km / 111.0, km / (111.0 * max(0.2, math.cos(math.radians(lat))))
    rows = c.execute("SELECT * FROM reports WHERE ts>=? AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
                     (since, lat - dlat, lat + dlat, lon - dlon, lon + dlon)).fetchall()
    return [report_row(r) for r in rows]


def exact_duplicates(c, dhash, since, max_ham, exclude=()):
    """Reports ANYWHERE (not only near the new report) whose photo hash is identical/near-identical. The same file uploaded from a
    different GPS position must still be recognised as the same observation."""
    out = []
    for r in c.execute("SELECT id, dhash FROM reports WHERE ts>=?", (since,)):
        if r["id"] in exclude or not r["dhash"]:
            continue
        if bin(int(r["dhash"], 16) ^ int(dhash)).count("1") <= max_ham:
            out.append(r["id"])
    if not out:
        return []
    q = ",".join("?" * len(out))
    return [report_row(r) for r in c.execute(f"SELECT * FROM reports WHERE id IN ({q})", out)]


def insert_report(c, r):
    cur = c.execute("""INSERT INTO reports(farmer_id,lat,lon,ts,crop,top_class,top_p,u,probs,emb,dhash,w,dep_score,cluster_id,image,action,post_p,
        n_eff,n_src,conflict,costs,repeat_kind,verify_status,note,pts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (r["farmer_id"], r["lat"], r["lon"], r["ts"], r["crop"], r["top_class"], r["top_p"], r["u"], json.dumps([float(x) for x in r["probs"]]),
         np.asarray(r["emb"], dtype=np.float32).tobytes(), format(int(r["dhash"]), "x"), r["w"], r["dep_score"], r.get("cluster_id"),
         r.get("image"), r.get("action"), r.get("post_p"), r.get("n_eff"), r.get("n_src"), r.get("conflict"),
         json.dumps(r["costs"]) if r.get("costs") else None, r.get("repeat_kind"), r.get("verify_status"), r.get("note"), r.get("pts")))
    return cur.lastrowid
