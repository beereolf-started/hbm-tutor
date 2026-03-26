from database import engine, Base, SessionLocal
from models import (User, Student, Section, Item, Attachment, Subject,
    Course, CourseSection, CourseSectionItem, CourseItemSubblock, ItemSubblock, Board, Message,
    parent_student_link, tutor_student_link)
from auth import hash_password
from sqlalchemy import text

def migrate(db):
    """Миграция v2.2: новые поля и типы элементов"""
    steps = [
        # Расширить enum itemtype новыми значениями
        "ALTER TYPE itemtype ADD VALUE IF NOT EXISTS 'media'",
        "ALTER TYPE itemtype ADD VALUE IF NOT EXISTS 'control'",
        "ALTER TYPE itemtype ADD VALUE IF NOT EXISTS 'idz'",
        # Изменить CourseSectionItem.type на varchar (для гибкости)
        "ALTER TABLE course_section_items ALTER COLUMN type TYPE varchar(50) USING type::varchar",
        # Новые колонки
        "ALTER TABLE items ADD COLUMN IF NOT EXISTS grade integer",
        "ALTER TABLE sections ADD COLUMN IF NOT EXISTS locked boolean NOT NULL DEFAULT false",
        "ALTER TABLE sections ADD COLUMN IF NOT EXISTS idz_text text",
        "ALTER TABLE course_sections ADD COLUMN IF NOT EXISTS idz_text text",
        "ALTER TABLE course_section_items ADD COLUMN IF NOT EXISTS total integer",
        "ALTER TABLE course_section_items ADD COLUMN IF NOT EXISTS text text",
        # v2.3: sub-blocks + media file fields
        "ALTER TABLE course_section_items ADD COLUMN IF NOT EXISTS file_path text",
        "ALTER TABLE course_section_items ADD COLUMN IF NOT EXISTS mime varchar(200)",
        "ALTER TABLE course_section_items ADD COLUMN IF NOT EXISTS size integer",
        "CREATE TABLE IF NOT EXISTS course_item_subblocks (id varchar(12) PRIMARY KEY, item_id varchar(12) NOT NULL REFERENCES course_section_items(id) ON DELETE CASCADE, type varchar(10) NOT NULL DEFAULT 'text', content text, name varchar(300), position integer NOT NULL DEFAULT 0, file_path text, mime varchar(200), size integer)",
        "CREATE TABLE IF NOT EXISTS item_subblocks (id varchar(12) PRIMARY KEY, item_id varchar(12) NOT NULL REFERENCES items(id) ON DELETE CASCADE, type varchar(10) NOT NULL DEFAULT 'text', content text, name varchar(300), position integer NOT NULL DEFAULT 0, file_path text, mime varchar(200), size integer)",
        # v2.4: tutor multi-subject support
        "CREATE TABLE IF NOT EXISTS tutor_subjects (tutor_id varchar(12) NOT NULL REFERENCES users(id) ON DELETE CASCADE, subject_id varchar(12) NOT NULL REFERENCES subjects(id) ON DELETE CASCADE, PRIMARY KEY (tutor_id, subject_id))",
        "INSERT INTO tutor_subjects (tutor_id, subject_id) SELECT id, subject_id FROM users WHERE role='tutor' AND subject_id IS NOT NULL ON CONFLICT DO NOTHING",
        # v2.5: messaging
        "CREATE TABLE IF NOT EXISTS messages (id varchar(12) PRIMARY KEY, from_id varchar(12) NOT NULL REFERENCES users(id) ON DELETE CASCADE, to_id varchar(12) NOT NULL REFERENCES users(id) ON DELETE CASCADE, text text NOT NULL, created_at timestamptz DEFAULT now(), is_read boolean NOT NULL DEFAULT false)",
        # v3.8: schedule
        "CREATE TABLE IF NOT EXISTS schedule_slots (id varchar(12) PRIMARY KEY, tutor_id varchar(12) NOT NULL REFERENCES users(id) ON DELETE CASCADE, student_id varchar(12) REFERENCES students(id) ON DELETE CASCADE, day_of_week integer NOT NULL, slot_index integer NOT NULL, duration integer NOT NULL DEFAULT 2, note varchar(300), color varchar(20), created_at timestamptz DEFAULT now())",
        # v3.9: duration field
        "ALTER TABLE schedule_slots ADD COLUMN IF NOT EXISTS duration integer NOT NULL DEFAULT 2",
        # v4.0: student note on schedule slots
        "ALTER TABLE schedule_slots ADD COLUMN IF NOT EXISTS student_note varchar(500)",
        # v4.1: rewards toggle per student
        "ALTER TABLE students ADD COLUMN IF NOT EXISTS rewards_enabled boolean NOT NULL DEFAULT true",
        # v4.2: extended student model — level + expanded goals
        "ALTER TABLE students ADD COLUMN IF NOT EXISTS level varchar(20) NOT NULL DEFAULT 'school'",
        "ALTER TABLE students ALTER COLUMN grade DROP NOT NULL",
        "ALTER TYPE goaltype ADD VALUE IF NOT EXISTS 'oge'",
        "ALTER TYPE goaltype ADD VALUE IF NOT EXISTS 'improve_grades'",
        "ALTER TYPE goaltype ADD VALUE IF NOT EXISTS 'deepening'",
        "ALTER TYPE goaltype ADD VALUE IF NOT EXISTS 'extra_education'",
        # v4.3: personal boards & shares (idempotent, in case create_all missed them)
        "CREATE TABLE IF NOT EXISTS personal_boards (id varchar(12) PRIMARY KEY, owner_id varchar(12) NOT NULL REFERENCES users(id) ON DELETE CASCADE, title varchar(300) NOT NULL DEFAULT 'Новая доска', strokes text DEFAULT '[]', share_token varchar(32) UNIQUE, created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now())",
        "CREATE TABLE IF NOT EXISTS personal_board_shares (board_id varchar(12) NOT NULL REFERENCES personal_boards(id) ON DELETE CASCADE, user_id varchar(12) NOT NULL REFERENCES users(id) ON DELETE CASCADE, PRIMARY KEY (board_id, user_id))",
        "CREATE TABLE IF NOT EXISTS board_invites (id varchar(12) PRIMARY KEY, board_id varchar(12) NOT NULL REFERENCES personal_boards(id) ON DELETE CASCADE, from_id varchar(12) NOT NULL REFERENCES users(id) ON DELETE CASCADE, to_id varchar(12) NOT NULL REFERENCES users(id) ON DELETE CASCADE, status varchar(20) NOT NULL DEFAULT 'pending', created_at timestamptz DEFAULT now())",
        "CREATE TABLE IF NOT EXISTS notifications (id varchar(12) PRIMARY KEY, user_id varchar(12) NOT NULL REFERENCES users(id) ON DELETE CASCADE, text text NOT NULL, is_read boolean NOT NULL DEFAULT false, created_at timestamptz DEFAULT now(), link varchar(500), notif_type varchar(50))",
        # v4.4: financial tracking
        "ALTER TABLE schedule_slots ADD COLUMN IF NOT EXISTS price numeric(10,2)",
        "CREATE TABLE IF NOT EXISTS board_presence_logs (id varchar(12) PRIMARY KEY, student_id varchar(12) NOT NULL REFERENCES students(id) ON DELETE CASCADE, user_id varchar(12) NOT NULL REFERENCES users(id) ON DELETE CASCADE, user_role varchar(20) NOT NULL, connected_at timestamptz DEFAULT now(), disconnected_at timestamptz)",
        "CREATE TABLE IF NOT EXISTS commission_bills (id varchar(12) PRIMARY KEY, tutor_id varchar(12) NOT NULL REFERENCES users(id) ON DELETE CASCADE, total_lessons numeric(10,2) NOT NULL DEFAULT 0, total_commission numeric(10,2) NOT NULL DEFAULT 0, status varchar(20) NOT NULL DEFAULT 'open', tutor_note varchar(1000), owner_note varchar(1000), reported_at timestamptz, closed_at timestamptz, created_at timestamptz DEFAULT now())",
        "CREATE TABLE IF NOT EXISTS lesson_sessions (id varchar(12) PRIMARY KEY, schedule_slot_id varchar(12) REFERENCES schedule_slots(id) ON DELETE SET NULL, bill_id varchar(12) REFERENCES commission_bills(id) ON DELETE SET NULL, tutor_id varchar(12) NOT NULL REFERENCES users(id) ON DELETE CASCADE, student_id varchar(12) NOT NULL REFERENCES students(id) ON DELETE CASCADE, scheduled_at timestamptz NOT NULL, price numeric(10,2), commission numeric(10,2), tutor_present boolean NOT NULL DEFAULT false, student_present boolean NOT NULL DEFAULT false, status varchar(20) NOT NULL DEFAULT 'auto', note varchar(500), created_at timestamptz DEFAULT now())",
    ]
    for sql in steps:
        try:
            db.execute(text(sql)); db.commit()
            print(f"  ✓ {sql[:60]}")
        except Exception as e:
            db.rollback()
            print(f"  ~ skip: {sql[:60]} ({type(e).__name__})")

def init():
    print("Создаю таблицы...")
    Base.metadata.create_all(bind=engine)
    for t in Base.metadata.sorted_tables: print(f"  ✓ {t.name}")
    db = SessionLocal()
    print("Миграция v2.2...")
    migrate(db)
    try:
        math = db.query(Subject).filter(Subject.name=="Математика").first()
        if not math:
            math = Subject(name="Математика", icon="📐")
            db.add(math); db.flush()
            print("\n  Создан предмет: 📐 Математика")
        owner = db.query(User).filter(User.role=="owner").first()
        if not owner:
            owner = User(login="admin", password_hash=hash_password("admin123"),
                role="owner", name="Владелец", must_change_password=True, subject_id=math.id)
            db.add(owner); db.commit()
            print("  Аккаунт владельца: admin / admin123")
        else:
            print(f"  Владелец уже есть: {owner.login}")
            db.commit()
    finally: db.close()
    print("Готово!")

if __name__ == "__main__": init()
