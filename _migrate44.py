import sys
sys.path.insert(0,'/opt/hbm')
from database import SessionLocal
from sqlalchemy import text
db=SessionLocal()
sqls=[
"ALTER TABLE schedule_slots ADD COLUMN IF NOT EXISTS price numeric(10,2)",
"CREATE TABLE IF NOT EXISTS board_presence_logs (id varchar(12) PRIMARY KEY, student_id varchar(12) NOT NULL REFERENCES students(id) ON DELETE CASCADE, user_id varchar(12) NOT NULL REFERENCES users(id) ON DELETE CASCADE, user_role varchar(20) NOT NULL, connected_at timestamptz DEFAULT now(), disconnected_at timestamptz)",
"CREATE TABLE IF NOT EXISTS commission_bills (id varchar(12) PRIMARY KEY, tutor_id varchar(12) NOT NULL REFERENCES users(id) ON DELETE CASCADE, total_lessons numeric(10,2) NOT NULL DEFAULT 0, total_commission numeric(10,2) NOT NULL DEFAULT 0, status varchar(20) NOT NULL DEFAULT 'open', tutor_note varchar(1000), owner_note varchar(1000), reported_at timestamptz, closed_at timestamptz, created_at timestamptz DEFAULT now())",
"CREATE TABLE IF NOT EXISTS lesson_sessions (id varchar(12) PRIMARY KEY, schedule_slot_id varchar(12) REFERENCES schedule_slots(id) ON DELETE SET NULL, bill_id varchar(12) REFERENCES commission_bills(id) ON DELETE SET NULL, tutor_id varchar(12) NOT NULL REFERENCES users(id) ON DELETE CASCADE, student_id varchar(12) NOT NULL REFERENCES students(id) ON DELETE CASCADE, scheduled_at timestamptz NOT NULL, price numeric(10,2), commission numeric(10,2), tutor_present boolean NOT NULL DEFAULT false, student_present boolean NOT NULL DEFAULT false, status varchar(20) NOT NULL DEFAULT 'auto', note varchar(500), created_at timestamptz DEFAULT now())",
]
for sql in sqls:
    try: db.execute(text(sql)); db.commit(); print('OK:',sql[:60])
    except Exception as e: db.rollback(); print('ERR:',str(e)[:100])
db.close()
