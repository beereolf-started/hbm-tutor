import asyncio
from fastapi import FastAPI,Depends,HTTPException,UploadFile,File,Form,WebSocket,WebSocketDisconnect,Request,Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session,joinedload
import os,json,jwt,secrets,subprocess,asyncio,shutil,mimetypes,tempfile,uuid
from datetime import datetime,timezone
from collections import defaultdict
from database import get_db,SessionLocal,engine,Base
from models import *
from schemas import *
from auth import (hash_password,verify_password,create_token,get_current_user,
    require_owner,require_tutor_or_owner,require_teamlead_or_owner,decode_token,SECRET_KEY,ALGORITHM)

app=FastAPI(title="HBM Репетитор API",version="2.0")
app.add_middleware(GZipMiddleware,minimum_size=512)

def _check_password(pw:str):
    """Валидация сложности пароля. Поднимает HTTPException при нарушении."""
    if len(pw)<8:
        raise HTTPException(400,"Пароль слишком короткий — минимум 8 символов")
    if len(set(pw))<3:
        raise HTTPException(400,"Пароль слишком простой — используйте разные символы")
    has_letter=any(c.isalpha() for c in pw)
    has_digit_or_special=any(not c.isalpha() for c in pw)
    if not (has_letter and has_digit_or_special):
        raise HTTPException(400,"Пароль должен содержать буквы и хотя бы одну цифру или спецсимвол")
app.add_middleware(CORSMiddleware,allow_origins=["*"],allow_methods=["*"],allow_headers=["*"])

RESEND_API_KEY=os.environ.get("RESEND_API_KEY")
EMAIL_FROM=os.environ.get("EMAIL_FROM","HBM Tutor <onboarding@resend.dev>")

def _send_email(to:str,subject:str,html:str):
    """Отправка письма через Resend.com. Если ключ не настроен — пишет в лог."""
    if not RESEND_API_KEY:
        print(f"[EMAIL not sent: no RESEND_API_KEY] to={to} subject={subject}")
        return False
    import requests
    try:
        r=requests.post("https://api.resend.com/emails",
            headers={"Authorization":f"Bearer {RESEND_API_KEY}","Content-Type":"application/json"},
            json={"from":EMAIL_FROM,"to":[to],"subject":subject,"html":html},timeout=10)
        if r.status_code>=400:
            print(f"[EMAIL ERROR] {r.status_code} {r.text}")
            return False
        return True
    except Exception as e:
        print(f"[EMAIL EXCEPTION] {e}")
        return False

FCM_CREDENTIALS_FILE=os.environ.get("FCM_CREDENTIALS_FILE")
_fcm_app=None
def _fcm_init():
    """Лениво инициализирует Firebase Admin SDK. None, если не настроено."""
    global _fcm_app
    if _fcm_app is not None:
        return _fcm_app
    if not FCM_CREDENTIALS_FILE or not os.path.exists(FCM_CREDENTIALS_FILE):
        return None
    try:
        import firebase_admin
        from firebase_admin import credentials
        _fcm_app=firebase_admin.initialize_app(credentials.Certificate(FCM_CREDENTIALS_FILE))
        return _fcm_app
    except Exception as e:
        print(f"[FCM INIT ERROR] {e}")
        return None

def send_push_to_user(db:Session,user_id:str,title:str,body:str,data:dict=None):
    """Отправляет push-уведомление на все зарегистрированные устройства пользователя. Тихо игнорирует ошибки/отсутствие настройки."""
    if _fcm_init() is None:
        return
    tokens=db.query(PushToken).filter(PushToken.user_id==user_id).all()
    if not tokens:
        return
    from firebase_admin import messaging
    for pt in tokens:
        try:
            messaging.send(messaging.Message(
                notification=messaging.Notification(title=title,body=body),
                data={k:str(v) for k,v in (data or {}).items()},
                token=pt.token,
            ))
        except Exception as e:
            err=str(e)
            if "not-found" in err.lower() or "unregistered" in err.lower() or "invalid-argument" in err.lower():
                db.query(PushToken).filter(PushToken.id==pt.id).delete()
                db.commit()
            else:
                print(f"[FCM SEND ERROR] {e}")

from sqlalchemy import text as _sa_text

# ── Lesson Groups Schemas ─────────────────────────────────────────────────────
class LessonGroupCreate(BaseModel):
    name: str
    subject_id: Optional[str] = None
    color: Optional[str] = None
    note: Optional[str] = None
    max_students: Optional[int] = None

class LessonGroupUpdate(BaseModel):
    name: Optional[str] = None
    subject_id: Optional[str] = None
    color: Optional[str] = None
    note: Optional[str] = None
    max_students: Optional[int] = None

class LessonGroupMemberAdd(BaseModel):
    student_id: str

class LessonGroupCourseLinkCreate(BaseModel):
    instance_id: str
    rate: int = 0

class LessonGroupCourseLinkUpdate(BaseModel):
    rate: int

# ── Study Plan Schemas ────────────────────────────────────────────────────────
class StudyPlanCreate(BaseModel):
    student_id: Optional[str] = None
    group_id: Optional[str] = None
    subject_id: Optional[str] = None
    goal: Optional[str] = None
    tutor_id: Optional[str] = None  # owner может указать чужого тьютора

class StudyPlanUpdate(BaseModel):
    subject_id: Optional[str] = None
    goal: Optional[str] = None
    status: Optional[str] = None


@app.on_event("startup")
async def _startup_migrate():
    """Ensure all tables exist (safe, idempotent)"""
    try: Base.metadata.create_all(bind=engine)
    except: pass
    _steps=[
        "CREATE TABLE IF NOT EXISTS personal_boards (id varchar(12) PRIMARY KEY, owner_id varchar(12) NOT NULL REFERENCES users(id) ON DELETE CASCADE, title varchar(300) NOT NULL DEFAULT 'Новая доска', strokes text DEFAULT '[]', share_token varchar(32) UNIQUE, created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now())",
        "CREATE TABLE IF NOT EXISTS personal_board_shares (board_id varchar(12) NOT NULL REFERENCES personal_boards(id) ON DELETE CASCADE, user_id varchar(12) NOT NULL REFERENCES users(id) ON DELETE CASCADE, PRIMARY KEY (board_id, user_id))",
        "CREATE TABLE IF NOT EXISTS board_invites (id varchar(12) PRIMARY KEY, board_id varchar(12) NOT NULL REFERENCES personal_boards(id) ON DELETE CASCADE, from_id varchar(12) NOT NULL REFERENCES users(id) ON DELETE CASCADE, to_id varchar(12) NOT NULL REFERENCES users(id) ON DELETE CASCADE, status varchar(20) NOT NULL DEFAULT 'pending', created_at timestamptz DEFAULT now())",
        "CREATE TABLE IF NOT EXISTS notifications (id varchar(12) PRIMARY KEY, user_id varchar(12) NOT NULL REFERENCES users(id) ON DELETE CASCADE, text text NOT NULL, is_read boolean NOT NULL DEFAULT false, created_at timestamptz DEFAULT now(), link varchar(500), notif_type varchar(50))",
        "ALTER TABLE course_section_items ADD COLUMN IF NOT EXISTS note TEXT",
        "ALTER TABLE course_section_items ADD COLUMN IF NOT EXISTS lang VARCHAR(20)",
        "ALTER TABLE items ADD COLUMN IF NOT EXISTS lang VARCHAR(20)",
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_enum WHERE enumlabel='code' AND enumtypid=(SELECT oid FROM pg_type WHERE typname='itemtype')) THEN ALTER TYPE itemtype ADD VALUE 'code'; END IF; END $$",
        "CREATE TABLE IF NOT EXISTS chat_groups (id varchar(12) PRIMARY KEY, name varchar(300) NOT NULL, created_by varchar(12) REFERENCES users(id) ON DELETE SET NULL, student_id varchar(12) REFERENCES students(id) ON DELETE CASCADE, created_at timestamptz DEFAULT now())",
        "CREATE TABLE IF NOT EXISTS chat_group_members (group_id varchar(12) NOT NULL REFERENCES chat_groups(id) ON DELETE CASCADE, user_id varchar(12) NOT NULL REFERENCES users(id) ON DELETE CASCADE, PRIMARY KEY (group_id, user_id))",
        "CREATE TABLE IF NOT EXISTS group_messages (id varchar(12) PRIMARY KEY, group_id varchar(12) NOT NULL REFERENCES chat_groups(id) ON DELETE CASCADE, from_id varchar(12) REFERENCES users(id) ON DELETE SET NULL, text text NOT NULL, created_at timestamptz DEFAULT now())",
        "CREATE TABLE IF NOT EXISTS group_message_reads (group_id varchar(12) NOT NULL REFERENCES chat_groups(id) ON DELETE CASCADE, user_id varchar(12) NOT NULL REFERENCES users(id) ON DELETE CASCADE, last_read_at timestamptz DEFAULT now(), PRIMARY KEY (group_id, user_id))",
        "ALTER TABLE chat_groups ADD COLUMN IF NOT EXISTS photo TEXT",
        "ALTER TABLE chat_groups ADD COLUMN IF NOT EXISTS tutor_id varchar(12) REFERENCES users(id) ON DELETE SET NULL",
        # Teamlead
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_enum WHERE enumlabel='teamlead' AND enumtypid=(SELECT oid FROM pg_type WHERE typname='userrole')) THEN ALTER TYPE userrole ADD VALUE 'teamlead'; END IF; END $$",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS teamlead_id varchar(12) REFERENCES users(id) ON DELETE SET NULL",
        """CREATE TABLE IF NOT EXISTS lesson_records (
            id varchar(12) PRIMARY KEY,
            tutor_id varchar(12) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            student_id varchar(12) NOT NULL REFERENCES students(id) ON DELETE CASCADE,
            slot_id varchar(12) REFERENCES schedule_slots(id) ON DELETE SET NULL,
            held_at timestamptz NOT NULL DEFAULT now(),
            duration_min integer NOT NULL DEFAULT 60,
            rate integer NOT NULL DEFAULT 1500,
            amount integer NOT NULL DEFAULT 1500,
            note text,
            created_by varchar(12) REFERENCES users(id) ON DELETE SET NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_lr_tutor_date ON lesson_records(tutor_id, held_at)",
        "CREATE INDEX IF NOT EXISTS idx_lr_student_date ON lesson_records(student_id, held_at)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS no_commission BOOLEAN DEFAULT FALSE",
        """CREATE TABLE IF NOT EXISTS teamlead_subscriptions (
            id varchar(12) PRIMARY KEY,
            teamlead_id varchar(12) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            starts_at timestamptz NOT NULL,
            ends_at timestamptz NOT NULL,
            plan varchar(50) NOT NULL DEFAULT 'monthly',
            price integer NOT NULL DEFAULT 0,
            is_active boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now()
        )""",
        # Этап 2: статусы занятий и оплаты
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname='lessonstatus') THEN CREATE TYPE lessonstatus AS ENUM ('conducted','cancelled','rescheduled'); END IF; END $$",
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname='paymentstatus') THEN CREATE TYPE paymentstatus AS ENUM ('unpaid','paid','disputed'); END IF; END $$",
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname='payermodel') THEN CREATE TYPE payermodel AS ENUM ('self','parent'); END IF; END $$",
        "ALTER TABLE lesson_records ADD COLUMN IF NOT EXISTS status lessonstatus NOT NULL DEFAULT 'conducted'",
        "ALTER TABLE lesson_records ADD COLUMN IF NOT EXISTS payment_status paymentstatus NOT NULL DEFAULT 'unpaid'",
        "ALTER TABLE lesson_records ADD COLUMN IF NOT EXISTS payment_confirmed_by varchar(12) REFERENCES users(id) ON DELETE SET NULL",
        "ALTER TABLE lesson_records ADD COLUMN IF NOT EXISTS payment_confirmed_at timestamptz",
        "ALTER TABLE lesson_records ADD COLUMN IF NOT EXISTS is_auto boolean NOT NULL DEFAULT false",
        "ALTER TABLE students ADD COLUMN IF NOT EXISTS payer_model payermodel NOT NULL DEFAULT 'self'",
        """CREATE TABLE IF NOT EXISTS student_payments (
            id varchar(12) PRIMARY KEY,
            student_id varchar(12) NOT NULL REFERENCES students(id) ON DELETE CASCADE,
            recorded_by varchar(12) REFERENCES users(id) ON DELETE SET NULL,
            amount integer NOT NULL,
            paid_at timestamptz NOT NULL DEFAULT now(),
            note text,
            created_at timestamptz NOT NULL DEFAULT now()
        )""",
        # Этап 3: процентная комиссия
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS commission_rate INTEGER NOT NULL DEFAULT 5",
        """CREATE TABLE IF NOT EXISTS commission_payments (
            id varchar(12) PRIMARY KEY,
            user_id varchar(12) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            amount integer NOT NULL,
            paid_at timestamptz NOT NULL DEFAULT now(),
            covers_lessons text,
            status subscriptionpaymentstatus NOT NULL DEFAULT 'pending',
            recorded_by varchar(12) REFERENCES users(id) ON DELETE SET NULL,
            note text,
            created_at timestamptz NOT NULL DEFAULT now()
        )""",
        # Этап 1: финансовая модель пользователя
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname='subscriptionmodel') THEN CREATE TYPE subscriptionmodel AS ENUM ('percent','fixed','none'); END IF; END $$",
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname='paymentmodel') THEN CREATE TYPE paymentmodel AS ENUM ('centralized','decentralized'); END IF; END $$",
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname='subscriptionpaymentstatus') THEN CREATE TYPE subscriptionpaymentstatus AS ENUM ('pending','confirmed'); END IF; END $$",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_model subscriptionmodel",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_tutor BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS payment_model paymentmodel",
        """CREATE TABLE IF NOT EXISTS tutor_subscriptions (
            id varchar(12) PRIMARY KEY,
            user_id varchar(12) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            amount_monthly integer NOT NULL DEFAULT 0,
            started_at timestamptz NOT NULL DEFAULT now(),
            ends_at timestamptz,
            is_active boolean NOT NULL DEFAULT true,
            note text,
            created_by varchar(12) REFERENCES users(id) ON DELETE SET NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )""",
        """CREATE TABLE IF NOT EXISTS subscription_payments (
            id varchar(12) PRIMARY KEY,
            subscription_id varchar(12) NOT NULL REFERENCES tutor_subscriptions(id) ON DELETE CASCADE,
            amount integer NOT NULL,
            period varchar(7) NOT NULL,
            paid_at timestamptz NOT NULL DEFAULT now(),
            recorded_by varchar(12) REFERENCES users(id) ON DELETE SET NULL,
            status subscriptionpaymentstatus NOT NULL DEFAULT 'pending',
            note text,
            created_at timestamptz NOT NULL DEFAULT now()
        )""",
        # Программы (экземпляры курсов) + сессии
        """CREATE TABLE IF NOT EXISTS course_instances (
            id varchar(12) PRIMARY KEY,
            title varchar(300) NOT NULL DEFAULT 'Программа',
            tutor_id varchar(12) REFERENCES users(id) ON DELETE SET NULL,
            course_id varchar(12) REFERENCES courses(id) ON DELETE SET NULL,
            subject_id varchar(12) REFERENCES subjects(id) ON DELETE SET NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )""",
        """CREATE TABLE IF NOT EXISTS enrollments (
            id varchar(12) PRIMARY KEY,
            instance_id varchar(12) NOT NULL REFERENCES course_instances(id) ON DELETE CASCADE,
            student_id varchar(12) NOT NULL REFERENCES students(id) ON DELETE CASCADE,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(instance_id, student_id)
        )""",
        "ALTER TABLE sections ADD COLUMN IF NOT EXISTS instance_id varchar(12) REFERENCES course_instances(id) ON DELETE SET NULL",
        "ALTER TABLE course_instances ADD COLUMN IF NOT EXISTS grade varchar(10)",
        "ALTER TABLE course_instances ADD COLUMN IF NOT EXISTS goal varchar(50)",
        # ── TZ: новые роли, рекрутинг, демо, антифрод ──────────────────
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_enum WHERE enumlabel='recruiter' AND enumtypid=(SELECT oid FROM pg_type WHERE typname='userrole')) THEN ALTER TYPE userrole ADD VALUE 'recruiter'; END IF; END $$",
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_enum WHERE enumlabel='demo_tutor' AND enumtypid=(SELECT oid FROM pg_type WHERE typname='userrole')) THEN ALTER TYPE userrole ADD VALUE 'demo_tutor'; END IF; END $$",
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_enum WHERE enumlabel='demo_teamlead' AND enumtypid=(SELECT oid FROM pg_type WHERE typname='userrole')) THEN ALTER TYPE userrole ADD VALUE 'demo_teamlead'; END IF; END $$",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS recruited_by varchar(12) REFERENCES users(id) ON DELETE SET NULL",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_demo BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS demo_expires_at TIMESTAMP",
        "ALTER TABLE students ADD COLUMN IF NOT EXISTS is_demo BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE lesson_records ADD COLUMN IF NOT EXISTS is_demo BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE schedule_slots ADD COLUMN IF NOT EXISTS is_demo BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE course_instances ADD COLUMN IF NOT EXISTS is_demo BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE schedule_slots ADD COLUMN IF NOT EXISTS instance_id VARCHAR(12) REFERENCES course_instances(id) ON DELETE SET NULL",
        # ── Планы занятий ──────────────────────────────────────────────────
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname='studyplanstatus') THEN CREATE TYPE studyplanstatus AS ENUM ('active','paused','done'); END IF; END $$",
        """CREATE TABLE IF NOT EXISTS study_plans (
            id varchar(12) PRIMARY KEY,
            tutor_id varchar(12) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            student_id varchar(12) REFERENCES students(id) ON DELETE CASCADE,
            group_id varchar(12) REFERENCES lesson_groups(id) ON DELETE CASCADE,
            subject_id varchar(12) REFERENCES subjects(id) ON DELETE SET NULL,
            goal text,
            status studyplanstatus NOT NULL DEFAULT 'active',
            created_at timestamptz NOT NULL DEFAULT now()
        )""",
        "ALTER TABLE course_instances ADD COLUMN IF NOT EXISTS study_plan_id varchar(12) REFERENCES study_plans(id) ON DELETE SET NULL",
        "CREATE INDEX IF NOT EXISTS idx_sp_tutor ON study_plans(tutor_id)",
        "CREATE INDEX IF NOT EXISTS idx_sp_student ON study_plans(student_id)",
        "CREATE INDEX IF NOT EXISTS idx_sp_group ON study_plans(group_id)",
        "ALTER TABLE boards ADD COLUMN IF NOT EXISTS instance_id VARCHAR(12) REFERENCES course_instances(id) ON DELETE SET NULL",
        "ALTER TABLE boards DROP CONSTRAINT IF EXISTS boards_student_id_key",
        "CREATE UNIQUE INDEX IF NOT EXISTS boards_student_general_uq ON boards(student_id) WHERE instance_id IS NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS boards_student_instance_uq ON boards(student_id,instance_id) WHERE instance_id IS NOT NULL",
        "CREATE TABLE IF NOT EXISTS recruitment_rewards (id varchar(12) PRIMARY KEY, recruiter_id varchar(12) NOT NULL REFERENCES users(id) ON DELETE CASCADE, recruited_user_id varchar(12) NOT NULL REFERENCES users(id) ON DELETE CASCADE, source_payment_type varchar(20) NOT NULL, source_payment_id varchar(12) NOT NULL, amount integer NOT NULL, status varchar(20) NOT NULL DEFAULT 'pending', confirmed_by varchar(12) REFERENCES users(id) ON DELETE SET NULL, confirmed_at timestamptz, created_at timestamptz NOT NULL DEFAULT now())",
        "CREATE TABLE IF NOT EXISTS board_anomaly_flags (id varchar(12) PRIMARY KEY, tutor_id varchar(12) NOT NULL REFERENCES users(id) ON DELETE CASCADE, student_id varchar(12) NOT NULL REFERENCES students(id) ON DELETE CASCADE, session_start timestamptz NOT NULL, session_duration_min integer NOT NULL DEFAULT 0, status varchar(20) NOT NULL DEFAULT 'open', dismissed_by varchar(12) REFERENCES users(id) ON DELETE SET NULL, dismissed_at timestamptz, note text, created_at timestamptz NOT NULL DEFAULT now())",
        "UPDATE users SET is_active=TRUE WHERE is_active IS NULL",
        "ALTER TABLE boards ALTER COLUMN student_id DROP NOT NULL",

        # ── Групповые занятия ──────────────────────────────────────────────────
        "CREATE TABLE IF NOT EXISTS lesson_groups (id VARCHAR(12) PRIMARY KEY, name VARCHAR(255) NOT NULL, tutor_id VARCHAR(12) NOT NULL REFERENCES users(id) ON DELETE CASCADE, subject_id VARCHAR(12) REFERENCES subjects(id) ON DELETE SET NULL, color VARCHAR(20), note TEXT, max_students INTEGER, is_demo BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
        "CREATE TABLE IF NOT EXISTS group_memberships (id VARCHAR(12) PRIMARY KEY, group_id VARCHAR(12) NOT NULL REFERENCES lesson_groups(id) ON DELETE CASCADE, student_id VARCHAR(12) NOT NULL REFERENCES students(id) ON DELETE CASCADE, joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), left_at TIMESTAMPTZ)",
        "CREATE UNIQUE INDEX IF NOT EXISTS gm_active_uq ON group_memberships(group_id, student_id) WHERE left_at IS NULL",
        "CREATE TABLE IF NOT EXISTS group_course_links (id VARCHAR(12) PRIMARY KEY, group_id VARCHAR(12) NOT NULL REFERENCES lesson_groups(id) ON DELETE CASCADE, instance_id VARCHAR(12) NOT NULL REFERENCES course_instances(id) ON DELETE CASCADE, rate INTEGER NOT NULL DEFAULT 0, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), UNIQUE(group_id, instance_id))",
        "ALTER TABLE schedule_slots ADD COLUMN IF NOT EXISTS group_id VARCHAR(12) REFERENCES lesson_groups(id) ON DELETE SET NULL",
        "ALTER TABLE lesson_records ADD COLUMN IF NOT EXISTS group_id VARCHAR(12) REFERENCES lesson_groups(id) ON DELETE SET NULL",
        "ALTER TABLE boards ADD COLUMN IF NOT EXISTS group_id VARCHAR(12) REFERENCES lesson_groups(id) ON DELETE CASCADE",
        "CREATE UNIQUE INDEX IF NOT EXISTS boards_group_instance_uq ON boards(group_id, instance_id) WHERE group_id IS NOT NULL",
        "ALTER TABLE course_instances ADD COLUMN IF NOT EXISTS group_id VARCHAR(12) REFERENCES lesson_groups(id) ON DELETE CASCADE",
        "ALTER TABLE board_anomaly_flags ADD COLUMN IF NOT EXISTS group_id VARCHAR(12) REFERENCES lesson_groups(id) ON DELETE SET NULL",
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'slot_student_or_group') THEN ALTER TABLE schedule_slots ADD CONSTRAINT slot_student_or_group CHECK (student_id IS NULL OR group_id IS NULL); END IF; END $$",
        "ALTER TABLE lesson_groups ADD COLUMN IF NOT EXISTS chat_group_id varchar(12) REFERENCES chat_groups(id) ON DELETE SET NULL",

        # ── Предметы ученика (многие-ко-многим) ──────────────────────────────
        "CREATE TABLE IF NOT EXISTS student_subjects (student_id VARCHAR(12) NOT NULL REFERENCES students(id) ON DELETE CASCADE, subject_id VARCHAR(12) NOT NULL REFERENCES subjects(id) ON DELETE CASCADE, PRIMARY KEY (student_id, subject_id))",

        # ── Цели ученика: кейсы (предмет + цель) ──────────────────────────
        "CREATE TABLE IF NOT EXISTS student_goal_cases (id VARCHAR(12) PRIMARY KEY, student_id VARCHAR(12) NOT NULL REFERENCES students(id) ON DELETE CASCADE, subject_id VARCHAR(12) REFERENCES subjects(id) ON DELETE SET NULL, goal VARCHAR(50) NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",

        # ── Обменник: скиллы тьютора + флаги видимости ─────────────────
        "CREATE TABLE IF NOT EXISTS tutor_skill_cases (id VARCHAR(12) PRIMARY KEY, tutor_id VARCHAR(12) NOT NULL REFERENCES users(id) ON DELETE CASCADE, subject_id VARCHAR(12) REFERENCES subjects(id) ON DELETE SET NULL, goal VARCHAR(50) NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_recruiting BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE students ADD COLUMN IF NOT EXISTS is_searching BOOLEAN NOT NULL DEFAULT FALSE",

        # ── Профиль: посты, комментарии, реакции ────────────────────────
        "CREATE TABLE IF NOT EXISTS profile_posts (id VARCHAR(12) PRIMARY KEY, author_id VARCHAR(12) NOT NULL REFERENCES users(id) ON DELETE CASCADE, content TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
        "CREATE TABLE IF NOT EXISTS post_comments (id VARCHAR(12) PRIMARY KEY, post_id VARCHAR(12) NOT NULL REFERENCES profile_posts(id) ON DELETE CASCADE, author_id VARCHAR(12) NOT NULL REFERENCES users(id) ON DELETE CASCADE, content TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
        "CREATE TABLE IF NOT EXISTS post_reactions (post_id VARCHAR(12) NOT NULL REFERENCES profile_posts(id) ON DELETE CASCADE, user_id VARCHAR(12) NOT NULL REFERENCES users(id) ON DELETE CASCADE, emoji VARCHAR(10) NOT NULL, PRIMARY KEY (post_id, user_id, emoji))",

        # ══ КУРСЫ v2 ══════════════════════════════════════════════════════
        "CREATE TABLE IF NOT EXISTS course_modules (id VARCHAR(12) PRIMARY KEY, course_id VARCHAR(12) NOT NULL REFERENCES courses_v2(id) ON DELETE CASCADE, title VARCHAR(300) NOT NULL DEFAULT 'Модуль', position INTEGER NOT NULL DEFAULT 0, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
        "ALTER TABLE course_modules ADD COLUMN IF NOT EXISTS unlock_threshold INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE course_modules ADD COLUMN IF NOT EXISTS points_per_lesson INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE course_lessons ADD COLUMN IF NOT EXISTS module_id VARCHAR(12) REFERENCES course_modules(id) ON DELETE SET NULL",
        "CREATE TABLE IF NOT EXISTS courses_v2 (id VARCHAR(12) PRIMARY KEY, title VARCHAR(300) NOT NULL DEFAULT 'Новый курс', description TEXT NOT NULL DEFAULT '', cover TEXT, subject_id VARCHAR(12) REFERENCES subjects(id) ON DELETE SET NULL, author_id VARCHAR(12) NOT NULL REFERENCES users(id) ON DELETE CASCADE, is_published BOOLEAN NOT NULL DEFAULT FALSE, storage_bytes BIGINT NOT NULL DEFAULT 0, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
        "CREATE TABLE IF NOT EXISTS course_lessons (id VARCHAR(12) PRIMARY KEY, course_id VARCHAR(12) NOT NULL REFERENCES courses_v2(id) ON DELETE CASCADE, title VARCHAR(300) NOT NULL DEFAULT 'Урок', estimated_min INTEGER NOT NULL DEFAULT 10, position INTEGER NOT NULL DEFAULT 0, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
        "CREATE TABLE IF NOT EXISTS lesson_blocks (id VARCHAR(12) PRIMARY KEY, lesson_id VARCHAR(12) NOT NULL REFERENCES course_lessons(id) ON DELETE CASCADE, type VARCHAR(20) NOT NULL DEFAULT 'text', position INTEGER NOT NULL DEFAULT 0, payload JSONB NOT NULL DEFAULT '{}', file_path TEXT, file_name VARCHAR(500), file_mime VARCHAR(200), file_size BIGINT NOT NULL DEFAULT 0, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
        "CREATE TABLE IF NOT EXISTS course_checkpoints (id VARCHAR(12) PRIMARY KEY, course_id VARCHAR(12) NOT NULL REFERENCES courses_v2(id) ON DELETE CASCADE, after_position INTEGER NOT NULL, title VARCHAR(300) NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
        "CREATE TABLE IF NOT EXISTS course_enrollments (id VARCHAR(12) PRIMARY KEY, course_id VARCHAR(12) NOT NULL REFERENCES courses_v2(id) ON DELETE CASCADE, student_id VARCHAR(12) NOT NULL REFERENCES students(id) ON DELETE CASCADE, tutor_id VARCHAR(12) REFERENCES users(id) ON DELETE SET NULL, enrolled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), UNIQUE(course_id, student_id))",
        "CREATE TABLE IF NOT EXISTS lesson_progress (enrollment_id VARCHAR(12) NOT NULL REFERENCES course_enrollments(id) ON DELETE CASCADE, lesson_id VARCHAR(12) NOT NULL REFERENCES course_lessons(id) ON DELETE CASCADE, done BOOLEAN NOT NULL DEFAULT FALSE, done_at TIMESTAMPTZ, PRIMARY KEY (enrollment_id, lesson_id))",
        "CREATE TABLE IF NOT EXISTS checkpoint_unlocks (enrollment_id VARCHAR(12) NOT NULL REFERENCES course_enrollments(id) ON DELETE CASCADE, checkpoint_id VARCHAR(12) NOT NULL REFERENCES course_checkpoints(id) ON DELETE CASCADE, unlocked_by VARCHAR(12) REFERENCES users(id) ON DELETE SET NULL, unlocked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), PRIMARY KEY (enrollment_id, checkpoint_id))",
        "CREATE TABLE IF NOT EXISTS quiz_answers (id VARCHAR(12) PRIMARY KEY, enrollment_id VARCHAR(12) NOT NULL REFERENCES course_enrollments(id) ON DELETE CASCADE, block_id VARCHAR(12) NOT NULL REFERENCES lesson_blocks(id) ON DELETE CASCADE, question_id VARCHAR(40) NOT NULL, answer JSONB NOT NULL, is_correct BOOLEAN, answered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), UNIQUE(enrollment_id, block_id, question_id))",
        # ── Автономная регистрация и подписки ─────────────────────────────────
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname='subscriptionstatus') THEN CREATE TYPE subscriptionstatus AS ENUM ('unverified','trial','pending_approval','active','pending_payment','expired'); END IF; END $$",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS email VARCHAR(200)",
        "CREATE UNIQUE INDEX IF NOT EXISTS users_email_uq ON users(email) WHERE email IS NOT NULL",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verify_token VARCHAR(64)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verify_expires_at TIMESTAMPTZ",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_ends_at TIMESTAMPTZ",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_status subscriptionstatus",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS commission_approved_by VARCHAR(12) REFERENCES users(id) ON DELETE SET NULL",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS commission_approved_at TIMESTAMPTZ",
        "ALTER TABLE lesson_records ADD COLUMN IF NOT EXISTS commission_status VARCHAR(20) NOT NULL DEFAULT 'not_applicable'",
        "ALTER TABLE lesson_records ADD COLUMN IF NOT EXISTS commission_amount INTEGER",
        # ── Push-уведомления (FCM) ─────────────────────────────────────────
        "CREATE TABLE IF NOT EXISTS push_tokens (id varchar(12) PRIMARY KEY, user_id varchar(12) NOT NULL REFERENCES users(id) ON DELETE CASCADE, token varchar(300) NOT NULL UNIQUE, platform varchar(20) NOT NULL DEFAULT 'android', created_at timestamptz NOT NULL DEFAULT now())",
        "CREATE INDEX IF NOT EXISTS push_tokens_user_idx ON push_tokens(user_id)",

    ]
    _db=SessionLocal()
    for _sql in _steps:
        try: _db.execute(_sa_text(_sql)); _db.commit()
        except: _db.rollback()

    # ── Миграция данных: student_courses + standalone sections → course_instances + enrollments ──
    try:
        # 1. Мигрируем существующие student_courses
        scs=_db.execute(_sa_text("SELECT id,student_id,tutor_id,title,created_at FROM student_courses")).fetchall()
        for sc in scs:
            exists=_db.execute(_sa_text("SELECT 1 FROM course_instances WHERE id=:id"),{"id":sc[0]}).fetchone()
            if not exists:
                _db.execute(_sa_text(
                    "INSERT INTO course_instances(id,title,tutor_id,created_at,updated_at) VALUES(:id,:title,:tid,:cat,:cat)"
                ),{"id":sc[0],"title":sc[1+2],"tid":sc[2],"cat":sc[4] or "now()"})
                enr_id=uuid.uuid4().hex[:12]
                _db.execute(_sa_text(
                    "INSERT INTO enrollments(id,instance_id,student_id) VALUES(:id,:iid,:sid) ON CONFLICT DO NOTHING"
                ),{"id":enr_id,"iid":sc[0],"sid":sc[1]})
                _db.execute(_sa_text(
                    "UPDATE sections SET instance_id=:iid WHERE course_id=:cid AND instance_id IS NULL"
                ),{"iid":sc[0],"cid":sc[0]})
        _db.commit()
    except Exception as e:
        _db.rollback(); print(f"[migrate student_courses] {e}")

    try:
        # 2. Мигрируем standalone-секции (course_id IS NULL, instance_id IS NULL)
        stids=_db.execute(_sa_text(
            "SELECT DISTINCT student_id FROM sections WHERE course_id IS NULL AND instance_id IS NULL"
        )).fetchall()
        for (sid,) in stids:
            # Проверяем, нет ли уже enrollment для этого студента без course_id
            already=_db.execute(_sa_text(
                "SELECT ci.id FROM course_instances ci JOIN enrollments e ON e.instance_id=ci.id "
                "WHERE e.student_id=:sid AND ci.course_id IS NULL LIMIT 1"
            ),{"sid":sid}).fetchone()
            if already:
                # уже есть, просто линкуем секции
                _db.execute(_sa_text(
                    "UPDATE sections SET instance_id=:iid WHERE student_id=:sid AND course_id IS NULL AND instance_id IS NULL"
                ),{"iid":already[0],"sid":sid})
            else:
                st=_db.execute(_sa_text("SELECT created_by FROM students WHERE id=:id"),{"id":sid}).fetchone()
                tutor_id=st[0] if st else None
                ci_id=uuid.uuid4().hex[:12]
                _db.execute(_sa_text(
                    "INSERT INTO course_instances(id,title,tutor_id) VALUES(:id,'Основная программа',:tid)"
                ),{"id":ci_id,"tid":tutor_id})
                enr_id=uuid.uuid4().hex[:12]
                _db.execute(_sa_text(
                    "INSERT INTO enrollments(id,instance_id,student_id) VALUES(:id,:iid,:sid) ON CONFLICT DO NOTHING"
                ),{"id":enr_id,"iid":ci_id,"sid":sid})
                _db.execute(_sa_text(
                    "UPDATE sections SET instance_id=:iid WHERE student_id=:sid AND course_id IS NULL AND instance_id IS NULL"
                ),{"iid":ci_id,"sid":sid})
        _db.commit()
    except Exception as e:
        _db.rollback(); print(f"[migrate standalone sections] {e}")

    try:
        # 3. Создаём "Основная программа" для студентов без единого enrollment
        orphan_students=_db.execute(_sa_text(
            "SELECT s.id, s.created_by FROM students s "
            "WHERE NOT EXISTS (SELECT 1 FROM enrollments e WHERE e.student_id=s.id)"
        )).fetchall()
        for (sid, created_by) in orphan_students:
            ci_id=uuid.uuid4().hex[:12]
            enr_id=uuid.uuid4().hex[:12]
            _db.execute(_sa_text(
                "INSERT INTO course_instances(id,title,tutor_id) VALUES(:id,'Основная программа',:tid)"
            ),{"id":ci_id,"tid":created_by})
            _db.execute(_sa_text(
                "INSERT INTO enrollments(id,instance_id,student_id) VALUES(:id,:iid,:sid) ON CONFLICT DO NOTHING"
            ),{"id":enr_id,"iid":ci_id,"sid":sid})
        _db.commit()
    except Exception as e:
        _db.rollback(); print(f"[migrate orphan students] {e}")

    # Remove old-style auto-groups (student_id set but no tutor_id = legacy 2-3 person groups)
    try:
        _db.execute(_sa_text("DELETE FROM chat_groups WHERE student_id IS NOT NULL AND tutor_id IS NULL"))
        _db.commit()
    except: _db.rollback()
    _db.close()
    # Sync auto-groups for all existing students (idempotent)
    _db2=SessionLocal()
    try:
        _stids=[r[0] for r in _db2.execute(_sa_text("SELECT id FROM students")).fetchall()]
        for _sid in _stids:
            try: _sync_auto_group(_sid,_db2)
            except: pass
    except: pass
    finally: _db2.close()
    asyncio.create_task(_auto_lesson_loop())
    asyncio.create_task(_demo_cleanup_loop())
    asyncio.create_task(_subscription_check_loop())

BASE_DIR=os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR=os.path.join(BASE_DIR,"uploads"); os.makedirs(UPLOAD_DIR,exist_ok=True)
def _up(aid,ext): return os.path.join(UPLOAD_DIR,f"{aid}{ext}"),f"uploads/{aid}{ext}"
def _abs(rel): return os.path.join(BASE_DIR,rel.replace("/",os.sep)) if rel else None
def _rm(rel):
    fp=_abs(rel)
    if fp and os.path.exists(fp): os.remove(fp)
def is_tr(u): return u.role in ("owner","tutor","teamlead")
def is_tl(u): return u.role in ("owner","teamlead")

def _is_subscription_restricted(u:User)->bool:
    if u.role not in ("tutor","teamlead"): return False
    status=getattr(u,"subscription_status",None)
    if not status: return False
    return status in ("pending_approval","pending_payment","expired")

def _team_tutor_ids(teamlead_id, db):
    """Возвращает список id тьюторов команды teamlead."""
    return [r.id for r in db.query(User.id).filter(User.teamlead_id==teamlead_id, User.role=="tutor").all()]

def _teamlead_student_ids(teamlead_id, db):
    """Возвращает все student.id доступные данному teamlead (через его тьюторов)."""
    tids = _team_tutor_ids(teamlead_id, db)
    if not tids: return []
    sids = set()
    for tid in tids:
        sids.update(_tutor_student_ids(tid, db))
    return list(sids)

def _notify_teamlead_or_owner(tutor, text, notif_type, link, db):
    """Уведомляет teamlead тьютора или owner, если тьютор независимый."""
    if tutor and tutor.teamlead_id:
        uid = tutor.teamlead_id
    else:
        owner = db.query(User).filter(User.role=="owner").first()
        uid = owner.id if owner else None
    if uid:
        n = Notification(user_id=uid, text=text, notif_type=notif_type, link=link)
        db.add(n)

# ═══ SUBSCRIPTION CHECK LOOP ═══
async def _subscription_check_loop():
    await asyncio.sleep(120)
    while True:
        try: _subscription_check_once()
        except Exception as _se: print(f"[subscription_check] {_se}")
        await asyncio.sleep(60*60)

def _subscription_check_once():
    from datetime import timedelta as _td
    db=SessionLocal()
    try:
        now=datetime.now(timezone.utc)
        owner=db.query(User).filter(User.role=="owner").first()
        expired_trials=db.query(User).filter(
            User.subscription_status=="trial",User.trial_ends_at<now).all()
        for u in expired_trials:
            if u.subscription_model=="percent":
                u.subscription_status="pending_approval"
                msg_o=f"Триал истёк у {u.name} ({u.role}). Ждёт вашего одобрения (комиссия 5%)."
                msg_u="Пробный период завершён. Ожидайте подтверждения от владельца."
            else:
                u.subscription_status="pending_payment"
                msg_o=f"Триал истёк у {u.name} ({u.role}). Ожидает оплаты подписки (1500 руб/мес)."
                msg_u="Пробный период завершён. Для продолжения оплатите подписку (1500 руб/мес)."
            if owner: db.add(Notification(user_id=owner.id,text=msg_o,notif_type="trial_expired",link="/hbm_tutor.html#subscriptions"))
            db.add(Notification(user_id=u.id,text=msg_u,notif_type="trial_expired",link="/hbm_tutor.html"))
        cutoff=now-_td(days=30)
        for u in db.query(User).filter(User.subscription_model=="percent",User.subscription_status=="active").all():
            if db.query(LessonRecord).filter(LessonRecord.tutor_id==u.id,LessonRecord.commission_status=="unpaid",LessonRecord.held_at<cutoff).first():
                u.subscription_status="expired"
                if owner: db.add(Notification(user_id=owner.id,text=f"{u.name} просрочил комиссию (>30 дней). Доступ заблокирован.",notif_type="commission_overdue",link="/hbm_tutor.html#subscriptions"))
                db.add(Notification(user_id=u.id,text="Выплата комиссии просрочена >30 дней. Доступ к доскам и звонкам ограничен.",notif_type="commission_overdue",link="/hbm_tutor.html"))
        db.commit()
    except Exception as e:
        db.rollback(); print(f"[sub_check] {e}")
    finally: db.close()

# ═══ LAST SEEN MIDDLEWARE ═══
@app.middleware("http")
async def update_last_seen(request: Request, call_next):
    response = await call_next(request)
    auth = request.headers.get("authorization","")
    if auth.startswith("Bearer "):
        try:
            payload = jwt.decode(auth[7:], SECRET_KEY, algorithms=[ALGORITHM])
            uid = payload.get("sub")
            if uid:
                db = SessionLocal()
                try:
                    u = db.query(User).filter(User.id==uid).first()
                    if u:
                        u.last_seen = datetime.now(timezone.utc)
                        db.commit()
                except: pass
                finally: db.close()
        except: pass
    return response

# ═══ AUTH ═══
@app.post("/api/auth/login",response_model=LoginResponse)
def login(d:LoginRequest,db:Session=Depends(get_db)):
    u=db.query(User).filter(User.login==d.login).first()
    if not u or not verify_password(d.password,u.password_hash): raise HTTPException(401,"Неверный логин или пароль")
    # Демо-аккаунт: проверка срока действия
    if u.role in ("demo_tutor","demo_teamlead") and u.demo_expires_at:
        from datetime import timezone as _tz
        _exp = u.demo_expires_at.replace(tzinfo=_tz.utc) if u.demo_expires_at.tzinfo is None else u.demo_expires_at
        if datetime.now(_tz.utc) > _exp:
            raise HTTPException(401,"Демо-период истёк")
    # Неактивированный аккаунт
    if not getattr(u,'is_active',True):
        raise HTTPException(401,"Аккаунт ещё не активирован. Обратитесь к администратору")
    return LoginResponse(token=create_token(u.id,u.role),role=u.role,name=u.name,must_change_password=u.must_change_password)

@app.post("/api/auth/change-password")
def change_pw(d:ChangePasswordRequest,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    if not verify_password(d.old_password,u.password_hash): raise HTTPException(400,"Неверный пароль")
    _check_password(d.new_password)
    u.password_hash=hash_password(d.new_password); u.must_change_password=False; db.commit()
    return {"ok":True}

@app.get("/api/auth/me",response_model=UserOut)
def me(u:User=Depends(get_current_user)): return u

@app.post("/api/auth/register",response_model=LoginResponse,status_code=201)
def register_student(d:StudentRegister,db:Session=Depends(get_db)):
    if db.query(User).filter(User.login==d.login).first():
        raise HTTPException(409,"Логин уже занят")
    _check_password(d.password)
    valid_grades=[str(i) for i in range(1,12)]+["university"]
    if d.grade not in valid_grades:
        raise HTTPException(400,"Неверный класс")
    valid_goals=[g.value for g in GoalType]
    if d.goal not in valid_goals:
        raise HTTPException(400,"Неверная цель")
    # Определяем основную цель: из первого кейса или из legacy поля
    primary_goal=d.goal
    if d.goal_cases and d.goal_cases[0].goal in [g.value for g in GoalType]:
        primary_goal=d.goal_cases[0].goal
    if primary_goal not in [g.value for g in GoalType]: primary_goal="ege"
    st=Student(name=d.name,grade=d.grade,goal=GoalType(primary_goal))
    # Основной предмет из первого кейса или legacy
    first_case_sid=d.goal_cases[0].subject_id if d.goal_cases else (d.subject_id or (d.subject_ids[0] if d.subject_ids else None))
    if first_case_sid:
        if db.query(Subject).filter(Subject.id==first_case_sid).first():
            st.subject_id=first_case_sid
    db.add(st); db.flush()
    # Сохраняем кейсы
    valid_goals_set=set(g.value for g in GoalType)
    for gc in d.goal_cases:
        if gc.goal not in valid_goals_set: continue
        sid=gc.subject_id or None
        if sid and not db.query(Subject).filter(Subject.id==sid).first(): sid=None
        cid=secrets.token_hex(6)
        db.execute(_sa_text("INSERT INTO student_goal_cases(id,student_id,subject_id,goal) VALUES(:id,:stid,:sid,:goal)").bindparams(id=cid,stid=st.id,sid=sid,goal=gc.goal))
    u=User(login=d.login,password_hash=hash_password(d.password),role=UserRole.student,name=d.name,must_change_password=False,student_id=st.id)
    db.add(u); db.commit(); db.refresh(u)
    return LoginResponse(token=create_token(u.id,u.role),role=u.role,name=u.name,must_change_password=False)


@app.post("/api/auth/register/pro",status_code=201)
def register_pro(d:ProRegister,db:Session=Depends(get_db)):
    d.login=d.login.strip(); d.email=d.email.strip().lower()
    if db.query(User).filter(User.login==d.login).first(): raise HTTPException(409,"Логин уже занят")
    if db.query(User).filter(User.email==d.email).first(): raise HTTPException(409,"Email уже зарегистрирован")
    _check_password(d.password)
    if d.role not in ("tutor","teamlead","recruiter","parent"): raise HTTPException(400,"Недопустимая роль")
    if d.role in ("tutor","teamlead") and d.subscription_type not in ("percent","fixed"):
        raise HTTPException(400,"Укажите тип подписки: percent (комиссия 5%) или fixed (1500 руб/мес)")
    import re as _re
    if not _re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$",d.email): raise HTTPException(400,"Некорректный email")
    verify_token=secrets.token_hex(32)
    from datetime import timedelta as _td
    sub_model=SubscriptionModel.percent if d.subscription_type=="percent" else (SubscriptionModel.fixed if d.subscription_type=="fixed" else None)
    u=User(login=d.login,password_hash=hash_password(d.password),role=UserRole(d.role),
           name=d.name,must_change_password=False,email=d.email,
           email_verified=False,email_verify_token=verify_token,
           email_verify_expires_at=datetime.now(timezone.utc)+_td(hours=24),
           is_active=False,subscription_status="unverified",subscription_model=sub_model)
    db.add(u); db.commit(); db.refresh(u)
    verify_url=f"https://hbmtutor.ru/verify-email.html?token={verify_token}"
    print(f"[VERIFY EMAIL] {d.email} -> {verify_url}")
    _send_email(d.email,"Подтверждение регистрации — HBM Tutor",
        f"""<div style="font-family:sans-serif;max-width:480px;margin:0 auto">
        <h2>Добро пожаловать в HBM Tutor!</h2>
        <p>Здравствуйте, {d.name}!</p>
        <p>Для завершения регистрации подтвердите ваш email:</p>
        <p><a href="{verify_url}" style="display:inline-block;padding:12px 24px;background:#3fb950;color:#fff;text-decoration:none;border-radius:8px;font-weight:600">Подтвердить email</a></p>
        <p style="color:#888;font-size:.9em">Если кнопка не работает, перейдите по ссылке: <a href="{verify_url}">{verify_url}</a></p>
        <p style="color:#888;font-size:.85em">Ссылка действительна 24 часа.</p>
        </div>""")
    owner=db.query(User).filter(User.role=="owner").first()
    if owner:
        rl={"tutor":"Репетитор","teamlead":"Тимлид","recruiter":"Рекрутёр","parent":"Родитель"}.get(d.role,d.role)
        sl=(" (комиссия 5%)" if d.subscription_type=="percent" else " (1500 руб/мес)") if d.role in ("tutor","teamlead") else ""
        db.add(Notification(user_id=owner.id,text=f"Новая регистрация: {rl} {d.name} <{d.email}>{sl}",notif_type="new_registration",link="/hbm_tutor.html#subscriptions")); db.commit()
    return {"ok":True,"message":"Проверьте почту для подтверждения регистрации"}

@app.get("/api/auth/verify-email")
def verify_email_endpoint(token:str,db:Session=Depends(get_db)):
    u=db.query(User).filter(User.email_verify_token==token).first()
    if not u: raise HTTPException(400,"Неверный или устаревший токен")
    exp=u.email_verify_expires_at
    if exp:
        exp_tz=exp.replace(tzinfo=timezone.utc) if exp.tzinfo is None else exp
        if datetime.now(timezone.utc)>exp_tz: raise HTTPException(400,"Ссылка устарела. Зарегистрируйтесь повторно.")
    from datetime import timedelta as _td
    u.email_verified=True; u.is_active=True
    u.email_verify_token=None; u.email_verify_expires_at=None
    if u.role in ("tutor","teamlead"):
        u.subscription_status="trial"; u.trial_ends_at=datetime.now(timezone.utc)+_td(days=7)
    else:
        u.subscription_status="active"
    db.commit()
    owner=db.query(User).filter(User.role=="owner").first()
    if owner:
        rl={"tutor":"Репетитор","teamlead":"Тимлид","recruiter":"Рекрутёр","parent":"Родитель"}.get(u.role.value,u.role.value)
        sl=(" (комиссия 5%)" if u.subscription_model=="percent" else " (1500 руб/мес)") if u.role in ("tutor","teamlead") else ""
        db.add(Notification(user_id=owner.id,text=f"{rl} {u.name} подтвердил email, начал триал{sl}",notif_type="trial_started",link="/hbm_tutor.html#subscriptions")); db.commit()
    return LoginResponse(token=create_token(u.id,u.role),role=u.role,name=u.name,must_change_password=False)

# ═══ USERS ═══
def _tutor_student_ids(uid,db):
    own=[s.id for s in db.query(Student).filter(Student.created_by==uid).all()]
    linked=[r.student_id for r in db.execute(tutor_student_link.select().where(tutor_student_link.c.tutor_id==uid)).fetchall()]
    return list(set(own+linked))

def _user_out(u,db):
    sids=[r.subject_id for r in db.execute(tutor_subject_link.select().where(tutor_subject_link.c.tutor_id==u.id)).fetchall()]
    return UserOut(id=u.id,login=u.login,role=u.role,name=u.name,must_change_password=u.must_change_password,
        student_id=u.student_id,subject_id=u.subject_id,created_at=u.created_at,subject_ids=sids,
        teamlead_id=u.teamlead_id,no_commission=bool(u.no_commission),
        subscription_model=u.subscription_model,commission_rate=u.commission_rate or 5,
        is_tutor=bool(u.is_tutor) if u.is_tutor is not None else False,
        payment_model=u.payment_model)


# ══ Авто-учёт занятий из расписания ══════════════════════════════════════════

async def _auto_lesson_loop():
    """Каждые 30 минут создаёт lesson_records для прошедших слотов расписания."""
    await asyncio.sleep(90)  # дать время серверу полностью запуститься
    while True:
        try:
            await _auto_create_lessons()
        except Exception as _ae:
            print(f"[auto_lessons] error: {_ae}")
        await asyncio.sleep(30 * 60)  # каждые 30 минут

async def _auto_create_lessons():
    from datetime import timedelta as _td
    _MSK = timezone(_td(hours=3))
    now_msk = datetime.now(_MSK)
    dow = now_msk.weekday()  # 0=Пн … 6=Вс (совпадает с day_of_week в расписании)
    current_slot = (now_msk.hour * 60 + now_msk.minute) // 30

    db = SessionLocal()
    try:
        slots = db.query(ScheduleSlot).filter(ScheduleSlot.day_of_week == dow).all()

        today_msk = now_msk.replace(hour=0, minute=0, second=0, microsecond=0)
        today_start_utc = today_msk.astimezone(timezone.utc)
        today_end_utc = (today_msk + _td(days=1)).astimezone(timezone.utc)

        created = 0
        for slot in slots:
            # Групповые слоты — обрабатываем отдельной веткой ниже
            if not slot.student_id and not slot.group_id:
                continue  # пустой слот
            # Ждём пока занятие закончится (slot_index + duration <= current_slot)
            slot_end_idx = slot.slot_index + (slot.duration or 2)
            if slot_end_idx > current_slot:
                continue

            # Проверяем: нет ли уже записи за сегодня для этого слота
            existing = db.query(LessonRecord).filter(
                LessonRecord.slot_id == slot.id,
                LessonRecord.held_at >= today_start_utc,
                LessonRecord.held_at < today_end_utc,
            ).first()
            if existing:
                continue

            # Ставка из Student.base_rate
            student = db.query(Student).filter(Student.id == slot.student_id).first()
            rate = student.base_rate if student else 1500
            duration_min = (slot.duration or 2) * 30

            # Время начала занятия (московское → UTC)
            h_min = slot.slot_index * 30
            held_at_msk = today_msk.replace(hour=h_min // 60, minute=h_min % 60)
            held_at_utc = held_at_msk.astimezone(timezone.utc)

            lr = LessonRecord(
                id=gen_id(), tutor_id=slot.tutor_id, student_id=slot.student_id,
                slot_id=slot.id, held_at=held_at_utc,
                duration_min=duration_min, rate=rate, amount=rate,
                is_auto=True, created_by=None,
            )
            db.add(lr)
            created += 1


        # ── Групповые слоты ──────────────────────────────────────────────────
        group_slots = [s for s in slots if s.group_id and not s.student_id]
        for slot in group_slots:
            slot_end_idx = slot.slot_index + (slot.duration or 2)
            if slot_end_idx > current_slot:
                continue
            # Дедупликация: если хоть одна запись по этому слоту за сегодня — пропускаем
            if db.query(LessonRecord).filter(
                LessonRecord.slot_id == slot.id,
                LessonRecord.held_at >= today_start_utc,
                LessonRecord.held_at < today_end_utc,
            ).first():
                continue
            # Активные участники группы
            members = db.query(GroupMembership).filter(
                GroupMembership.group_id == slot.group_id,
                GroupMembership.left_at == None
            ).all()
            if not members:
                continue
            # Ставка из GroupCourseLink (по instance_id слота)
            link = db.query(GroupCourseLink).filter(
                GroupCourseLink.group_id == slot.group_id,
                GroupCourseLink.instance_id == slot.instance_id
            ).first() if slot.instance_id else None
            rate = link.rate if link else 0
            duration_min = (slot.duration or 2) * 30
            h_min = slot.slot_index * 30
            held_at_msk = today_msk.replace(hour=h_min // 60, minute=h_min % 60)
            held_at_utc = held_at_msk.astimezone(timezone.utc)
            for member in members:
                lr = LessonRecord(
                    id=gen_id(), tutor_id=slot.tutor_id, student_id=member.student_id,
                    group_id=slot.group_id, slot_id=slot.id, held_at=held_at_utc,
                    duration_min=duration_min, rate=rate, amount=rate,
                    is_auto=True, created_by=None,
                )
                db.add(lr)
                created += 1
        db.commit()
        if created:
            print(f"[auto_lessons] {now_msk.strftime('%H:%M')} dow={dow}: создано {created} записей")
    except Exception as _e:
        db.rollback()
        raise
    finally:
        db.close()

@app.get("/api/users",response_model=list[UserOut])
def list_users(u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    if u.role=="owner": users=db.query(User).order_by(User.created_at.desc()).all()
    elif u.role=="teamlead":
        # Тьюторы команды + их студенты/родители
        tids=_team_tutor_ids(u.id,db)
        sids=_teamlead_student_ids(u.id,db)
        student_uids=[r.id for r in db.query(User.id).filter(User.student_id.in_(sids)).all()] if sids else []
        parent_ids=[r.parent_id for r in db.execute(parent_student_link.select().where(parent_student_link.c.student_id.in_(sids))).fetchall()] if sids else []
        visible=set(tids)|set(student_uids)|set(parent_ids)|{u.id}
        users=db.query(User).filter(User.id.in_(visible)).order_by(User.created_at.desc()).all()
    else:
        stids=_tutor_student_ids(u.id,db)
        student_uids=[r.id for r in db.query(User.id).filter(User.student_id.in_(stids)).all()] if stids else []
        parent_ids=[r.parent_id for r in db.execute(parent_student_link.select().where(parent_student_link.c.student_id.in_(stids))).fetchall()] if stids else []
        visible=set(student_uids)|set(parent_ids)|{u.id}
        users=db.query(User).filter(User.id.in_(visible)).order_by(User.created_at.desc()).all()
    return [_user_out(usr,db) for usr in users]

@app.post("/api/users",response_model=UserOut,status_code=201)
def create_user(d:UserCreate,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    if d.role=="tutor" and u.role not in ("owner","teamlead","recruiter"): raise HTTPException(403,"Только владелец, teamlead или рекрутёр может создавать преподавателей")
    if d.role=="board_user" and u.role!="owner": raise HTTPException(403,"Только владелец может создавать пользователей доски")
    if d.role=="teamlead" and u.role not in ("owner","recruiter"): raise HTTPException(403,"Только владелец или рекрутёр может создавать teamlead")
    if d.role=="recruiter" and u.role!="owner": raise HTTPException(403,"Только владелец может создавать рекрутёров")
    if d.role not in ("tutor","student","parent","board_user","teamlead","recruiter"): raise HTTPException(400,"Роль: tutor/student/parent/board_user/teamlead/recruiter")
    _conflict=db.query(User).filter(User.login==d.login).first()
    if _conflict: raise HTTPException(409,{"msg":"Логин занят","conflict":{"id":_conflict.id,"name":_conflict.name,"role":_conflict.role}})
    _check_password(d.password)
    # Tutor can only link parent to their own students
    if d.role=="parent" and d.children_ids and u.role=="tutor":
        allowed=set(_tutor_student_ids(u.id,db))
        for sid in d.children_ids:
            if sid not in allowed: raise HTTPException(403,"Нет доступа к этому ученику")
    nu=User(login=d.login,password_hash=hash_password(d.password),role=d.role,name=d.name,must_change_password=True)
    # Приглашённый тьютор teamlead — привязываем к его команде
    if d.role=="tutor" and u.role=="teamlead":
        nu.teamlead_id=u.id
    # teamlead может также добавить parent/student в рамках своих тьюторов
    if d.role=="parent" and u.role=="teamlead":
        if d.children_ids:
            allowed=set(_teamlead_student_ids(u.id,db))
            for sid in d.children_ids:
                if sid not in allowed: raise HTTPException(403,"Нет доступа к этому ученику")
    sids=d.subject_ids or ([d.subject_id] if d.subject_id else [])
    if d.role=="tutor" and sids:
        nu.subject_id=sids[0]
    if d.role=="student" and d.student_id:
        if not db.query(Student).filter(Student.id==d.student_id).first(): raise HTTPException(404)
        nu.student_id=d.student_id
    db.add(nu); db.flush()
    if d.role=="tutor" and sids:
        for sid in sids:
            if db.query(Subject).filter(Subject.id==sid).first():
                db.execute(tutor_subject_link.insert().values(tutor_id=nu.id,subject_id=sid))
    if d.role=="parent" and d.children_ids:
        for sid in d.children_ids:
            if db.query(Student).filter(Student.id==sid).first():
                db.execute(parent_student_link.insert().values(parent_id=nu.id,student_id=sid))
    db.commit(); db.refresh(nu)
    if d.role=="student" and nu.student_id:
        try: _sync_auto_group(nu.student_id,db)
        except: pass
    if d.role=="parent" and d.children_ids:
        for sid in d.children_ids:
            try: _sync_auto_group(sid,db)
            except: pass
    return nu

@app.post("/api/users/{uid}/link-student/{stid}",status_code=200)
def link_user_student(uid:str,stid:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    t=db.query(User).filter(User.id==uid,User.role=="student").first()
    if not t: raise HTTPException(404,"Пользователь не найден или не является учеником")
    if not db.query(Student).filter(Student.id==stid).first(): raise HTTPException(404,"Профиль ученика не найден")
    if u.role=="tutor":
        allowed=set(_tutor_student_ids(u.id,db))
        if stid not in allowed: raise HTTPException(403,"Нет доступа к этому ученику")
    t.student_id=stid; db.commit()
    return {"ok":True}

@app.delete("/api/users/{uid}/unlink-student",status_code=200)
def unlink_user_student(uid:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    t=db.query(User).filter(User.id==uid).first()
    if not t: raise HTTPException(404)
    if u.role=="tutor":
        stids=set(_tutor_student_ids(u.id,db))
        student_uids={r.id for r in db.query(User.id).filter(User.student_id.in_(stids)).all()} if stids else set()
        if t.id not in student_uids: raise HTTPException(403,"Нет доступа")
    t.student_id=None; db.commit()
    return {"ok":True}

@app.delete("/api/users/{uid}",status_code=204)
def del_user(uid:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    t=db.query(User).filter(User.id==uid).first()
    if not t: raise HTTPException(404)
    if t.role=="owner": raise HTTPException(400,"Нельзя удалить владельца")
    if t.role=="teamlead" and u.role!="owner": raise HTTPException(403,"Только владелец может удалять teamlead")
    if t.role=="tutor" and u.role not in ("owner","teamlead"): raise HTTPException(403,"Только владелец или teamlead может удалять преподавателей")
    if u.role=="teamlead":
        # Teamlead может удалять только членов своей команды
        allowed_tids=set(_team_tutor_ids(u.id,db))
        allowed_sids=set(_teamlead_student_ids(u.id,db))
        allowed_suids={r.id for r in db.query(User.id).filter(User.student_id.in_(allowed_sids)).all()} if allowed_sids else set()
        allowed_pids={r.parent_id for r in db.execute(parent_student_link.select().where(parent_student_link.c.student_id.in_(allowed_sids))).fetchall()} if allowed_sids else set()
        if t.id not in allowed_tids|allowed_suids|allowed_pids: raise HTTPException(403,"Нет доступа")
    if u.role=="tutor":
        # Tutor can only delete users visible to them
        stids=_tutor_student_ids(u.id,db)
        student_uids={r.id for r in db.query(User.id).filter(User.student_id.in_(stids)).all()} if stids else set()
        parent_ids={r.parent_id for r in db.execute(parent_student_link.select().where(parent_student_link.c.student_id.in_(stids))).fetchall()} if stids else set()
        if t.id not in student_uids|parent_ids: raise HTTPException(403,"Нет доступа")
    stid=t.student_id if t.role=="student" else None
    db.delete(t); db.flush()
    if stid and not db.query(User).filter(User.student_id==stid).count():
        s=db.query(Student).filter(Student.id==stid).first()
        if s: _cln_stu(s,db); db.delete(s)
    db.commit()

@app.get("/api/users/by-login/{login}")
def find_user_by_login(login:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    """Найти пользователя по точному логину (только для owner/teamlead — для диагностики конфликтов)."""
    if u.role not in ("owner","teamlead"): raise HTTPException(403)
    t=db.query(User).filter(User.login==login).first()
    if not t: raise HTTPException(404,"Пользователь с таким логином не найден")
    return _user_out(t,db)

# ═══ TUTOR SUBJECTS ═══
@app.get("/api/users/{uid}/subjects")
def get_tutor_subjects(uid:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    t=db.query(User).filter(User.id==uid,User.role=="tutor").first()
    if not t: raise HTTPException(404)
    sids=[r.subject_id for r in db.execute(tutor_subject_link.select().where(tutor_subject_link.c.tutor_id==uid)).fetchall()]
    subjs=db.query(Subject).filter(Subject.id.in_(sids)).all() if sids else []
    return [{"id":s.id,"name":s.name,"icon":s.icon} for s in subjs]

@app.post("/api/users/{uid}/subjects/{sid}",status_code=200)
def add_tutor_subject(uid:str,sid:str,o:User=Depends(require_owner),db:Session=Depends(get_db)):
    if not db.query(User).filter(User.id==uid,User.role=="tutor").first(): raise HTTPException(404)
    if not db.query(Subject).filter(Subject.id==sid).first(): raise HTTPException(404)
    if not db.execute(tutor_subject_link.select().where((tutor_subject_link.c.tutor_id==uid)&(tutor_subject_link.c.subject_id==sid))).first():
        db.execute(tutor_subject_link.insert().values(tutor_id=uid,subject_id=sid)); db.commit()
    return {"ok":True}

@app.delete("/api/users/{uid}/subjects/{sid}",status_code=200)
def del_tutor_subject(uid:str,sid:str,o:User=Depends(require_owner),db:Session=Depends(get_db)):
    db.execute(tutor_subject_link.delete().where((tutor_subject_link.c.tutor_id==uid)&(tutor_subject_link.c.subject_id==sid))); db.commit()
    return {"ok":True}

# ═══ SUBJECTS ═══
@app.get("/api/subjects/public")
def list_subj_public(db:Session=Depends(get_db)):
    return [{"id":s.id,"name":s.name,"icon":s.icon} for s in db.query(Subject).order_by(Subject.name).all()]

@app.get("/api/subjects",response_model=list[SubjectOut])
def list_subj(u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    return db.query(Subject).order_by(Subject.name).all()
@app.post("/api/subjects",response_model=SubjectOut,status_code=201)
def create_subj(d:SubjectCreate,o:User=Depends(require_owner),db:Session=Depends(get_db)):
    if db.query(Subject).filter(Subject.name==d.name).first(): raise HTTPException(409)
    s=Subject(name=d.name,icon=d.icon); db.add(s); db.commit(); db.refresh(s); return s
@app.patch("/api/subjects/{sid}",response_model=SubjectOut)
def upd_subj(sid:str,d:SubjectUpdate,o:User=Depends(require_owner),db:Session=Depends(get_db)):
    s=db.query(Subject).filter(Subject.id==sid).first()
    if not s: raise HTTPException(404)
    for f,v in d.model_dump(exclude_unset=True).items(): setattr(s,f,v)
    db.commit(); db.refresh(s); return s
@app.delete("/api/subjects/{sid}",status_code=204)
def del_subj(sid:str,o:User=Depends(require_owner),db:Session=Depends(get_db)):
    s=db.query(Subject).filter(Subject.id==sid).first()
    if not s: raise HTTPException(404)
    db.delete(s); db.commit()

# ═══ COURSES ═══
@app.get("/api/courses",response_model=list[CourseListItem])
def list_courses(subject_id:str=None,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    q=db.query(Course).options(joinedload(Course.author),joinedload(Course.subject))
    if subject_id: q=q.filter(Course.subject_id==subject_id)
    if u.role in ("tutor","teamlead","recruiter","demo_tutor","demo_teamlead"): q=q.filter((Course.access.in_(["public","internal"]))|(Course.author_id==u.id))
    return [CourseListItem(id=c.id,subject_id=c.subject_id,author_id=c.author_id,title=c.title,
        description=c.description,access=c.access,author_name=c.author.name if c.author else None,
        subject_name=c.subject.name if c.subject else None,sections_count=len(c.sections),
        created_at=c.created_at) for c in q.order_by(Course.updated_at.desc()).all()]
@app.get("/api/platform-courses",response_model=list[CourseListItem])
def list_platform_courses(u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    q=db.query(Course).options(joinedload(Course.sections),joinedload(Course.author),joinedload(Course.subject))
    q=q.filter(Course.access=="public")
    return [CourseListItem(id=c.id,subject_id=c.subject_id,author_id=c.author_id,title=c.title,
        description=c.description,access=c.access,author_name=c.author.name if c.author else None,
        subject_name=c.subject.name if c.subject else None,sections_count=len(c.sections),
        created_at=c.created_at) for c in q.order_by(Course.updated_at.desc()).all()]

@app.get("/api/platform-courses/{cid}")
def get_platform_course(cid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    c=db.query(Course).options(
        joinedload(Course.sections).joinedload(CourseSection.items).joinedload(CourseSectionItem.subblocks)
    ).filter(Course.id==cid,Course.access.in_(["public","internal"])).first()
    if not c: raise HTTPException(404)
    return c

# ═══ STUDENT PLATFORM COURSES (assigned extra courses) ═══
@app.get("/api/students/{stid}/platform-courses")
def get_student_platform_courses(stid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    """Courses assigned to this student as extra material."""
    st=db.query(Student).filter(Student.id==stid).first()
    if not st: raise HTTPException(404)
    # Check access
    if u.role=="student":
        if u.student_id!=stid: raise HTTPException(403)
    elif u.role=="parent":
        rows=db.execute(parent_student_link.select().where((parent_student_link.c.parent_id==u.id)&(parent_student_link.c.student_id==stid))).first()
        if not rows: raise HTTPException(403)
    rows=db.execute(student_platform_courses.select().where(student_platform_courses.c.student_id==stid)).fetchall()
    cids=[r.course_id for r in rows]
    if not cids: return []
    courses=db.query(Course).options(joinedload(Course.author),joinedload(Course.subject)).filter(Course.id.in_(cids)).all()
    return [CourseListItem(id=c.id,subject_id=c.subject_id,author_id=c.author_id,title=c.title,
        description=c.description,access=c.access,author_name=c.author.name if c.author else None,
        subject_name=c.subject.name if c.subject else None,sections_count=len(c.sections),
        created_at=c.created_at) for c in courses]

@app.post("/api/students/{stid}/platform-courses/{cid}",status_code=200)
def assign_student_course(stid:str,cid:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    if not db.query(Student).filter(Student.id==stid).first(): raise HTTPException(404)
    c=db.query(Course).filter(Course.id==cid).first()
    if not c: raise HTTPException(404)
    if c.access=="private" and c.author_id!=u.id and u.role!="owner": raise HTTPException(403)
    if u.role=="tutor":
        allowed=set(_tutor_student_ids(u.id,db))
        if stid not in allowed: raise HTTPException(403)
    exists=db.execute(student_platform_courses.select().where(
        (student_platform_courses.c.student_id==stid)&(student_platform_courses.c.course_id==cid))).first()
    if not exists:
        db.execute(student_platform_courses.insert().values(student_id=stid,course_id=cid))
        db.commit()
    return {"ok":True}

@app.delete("/api/students/{stid}/platform-courses/{cid}",status_code=200)
def unassign_student_course(stid:str,cid:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    if u.role=="tutor":
        allowed=set(_tutor_student_ids(u.id,db))
        if stid not in allowed: raise HTTPException(403)
    db.execute(student_platform_courses.delete().where(
        (student_platform_courses.c.student_id==stid)&(student_platform_courses.c.course_id==cid)))
    db.commit(); return {"ok":True}

@app.post("/api/me/platform-courses/{cid}",status_code=200)
def self_assign_course(cid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    """Student self-assigns a public course."""
    if u.role not in ("student","parent"): raise HTTPException(403,"Только для учеников")
    if not u.student_id: raise HTTPException(400,"Профиль ученика не привязан")
    c=db.query(Course).filter(Course.id==cid).first()
    if not c: raise HTTPException(404)
    if c.access!="public": raise HTTPException(403,"Курс недоступен")
    exists=db.execute(student_platform_courses.select().where(
        (student_platform_courses.c.student_id==u.student_id)&(student_platform_courses.c.course_id==cid))).first()
    if not exists:
        db.execute(student_platform_courses.insert().values(student_id=u.student_id,course_id=cid))
        db.commit()
    return {"ok":True}

@app.delete("/api/me/platform-courses/{cid}",status_code=200)
def self_unassign_course(cid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    if u.role not in ("student","parent"): raise HTTPException(403)
    if not u.student_id: raise HTTPException(400)
    db.execute(student_platform_courses.delete().where(
        (student_platform_courses.c.student_id==u.student_id)&(student_platform_courses.c.course_id==cid)))
    db.commit(); return {"ok":True}

@app.get("/api/courses/{cid}",response_model=CourseOut)
def get_course(cid:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    c=db.query(Course).options(
        joinedload(Course.sections).joinedload(CourseSection.items).joinedload(CourseSectionItem.subblocks)
    ).filter(Course.id==cid).first()
    if not c: raise HTTPException(404)
    if c.access=="private" and u.role!="owner" and c.author_id!=u.id: raise HTTPException(403)
    return c
@app.post("/api/courses",response_model=CourseOut,status_code=201)
def create_course(d:CourseCreate,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    if not db.query(Subject).filter(Subject.id==d.subject_id).first(): raise HTTPException(404)
    c=Course(subject_id=d.subject_id,author_id=u.id,title=d.title,description=d.description,access=d.access)
    db.add(c); db.commit(); db.refresh(c); return c
@app.patch("/api/courses/{cid}",response_model=CourseOut)
def upd_course(cid:str,d:CourseUpdate,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    c=db.query(Course).filter(Course.id==cid).first()
    if not c: raise HTTPException(404)
    if c.author_id!=u.id and u.role!="owner": raise HTTPException(403)
    if _is_subscription_restricted(u) and getattr(d,"access",None) and d.access!="private":
        raise HTTPException(403,"Публикация курса недоступна — требуется активная подписка")
    for f,v in d.model_dump(exclude_unset=True).items(): setattr(c,f,v)
    db.commit(); db.refresh(c); return c
@app.delete("/api/courses/{cid}",status_code=204)
def del_course(cid:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    c=db.query(Course).filter(Course.id==cid).first()
    if not c: raise HTTPException(404)
    if c.author_id!=u.id and u.role!="owner": raise HTTPException(403)
    db.delete(c); db.commit()

@app.patch("/api/course-items/{iid}",response_model=CourseSectionItemOut)
def upd_citem_patch(iid:str,d:CourseSectionItemUpdate,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    it=db.query(CourseSectionItem).filter(CourseSectionItem.id==iid).first()
    if not it: raise HTTPException(404)
    sec=db.query(CourseSection).filter(CourseSection.id==it.section_id).first()
    c=db.query(Course).filter(Course.id==sec.course_id).first()
    if c.author_id!=u.id and u.role!="owner": raise HTTPException(403)
    for f,v in d.model_dump(exclude_unset=True).items(): setattr(it,f,v)
    db.commit(); db.refresh(it); return it

# Course Sections
@app.post("/api/courses/{cid}/sections",response_model=CourseSectionOut,status_code=201)
def create_csec(cid:str,d:CourseSectionCreate,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    c=db.query(Course).filter(Course.id==cid).first()
    if not c: raise HTTPException(404)
    if c.author_id!=u.id and u.role!="owner": raise HTTPException(403)
    mp=db.query(CourseSection.position).filter(CourseSection.course_id==cid).order_by(CourseSection.position.desc()).first()
    sec=CourseSection(course_id=cid,title=d.title,position=(mp[0]+1) if mp else 0,idz_enabled=d.idz_enabled,control_enabled=d.control_enabled,idz_text=d.idz_text)
    db.add(sec); db.flush()
    for i,it in enumerate(d.items): db.add(CourseSectionItem(section_id=sec.id,type=it.type,position=i,name=it.name,total=it.total,text=it.text))
    db.commit(); db.refresh(sec); return sec
@app.patch("/api/course-sections/{sid}",response_model=CourseSectionOut)
def upd_csec(sid:str,d:CourseSectionUpdate,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    sec=db.query(CourseSection).filter(CourseSection.id==sid).first()
    if not sec: raise HTTPException(404)
    c=db.query(Course).filter(Course.id==sec.course_id).first()
    if c.author_id!=u.id and u.role!="owner": raise HTTPException(403)
    for f,v in d.model_dump(exclude_unset=True).items(): setattr(sec,f,v)
    db.commit(); db.refresh(sec); return sec
@app.delete("/api/course-sections/{sid}",status_code=204)
def del_csec(sid:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    sec=db.query(CourseSection).filter(CourseSection.id==sid).first()
    if not sec: raise HTTPException(404)
    c=db.query(Course).filter(Course.id==sec.course_id).first()
    if c.author_id!=u.id and u.role!="owner": raise HTTPException(403)
    db.delete(sec); db.commit()

# Course Items
@app.post("/api/course-sections/{sid}/items",response_model=CourseSectionItemOut,status_code=201)
def create_citem(sid:str,d:CourseSectionItemCreate,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    sec=db.query(CourseSection).filter(CourseSection.id==sid).first()
    if not sec: raise HTTPException(404)
    c=db.query(Course).filter(Course.id==sec.course_id).first()
    if c.author_id!=u.id and u.role!="owner": raise HTTPException(403)
    mp=db.query(CourseSectionItem.position).filter(CourseSectionItem.section_id==sid).order_by(CourseSectionItem.position.desc()).first()
    it=CourseSectionItem(section_id=sid,type=d.type,position=(mp[0]+1) if mp else 0,name=d.name)
    db.add(it); db.commit(); db.refresh(it); return it
@app.delete("/api/course-items/{iid}",status_code=204)
def del_citem(iid:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    it=db.query(CourseSectionItem).filter(CourseSectionItem.id==iid).first()
    if not it: raise HTTPException(404)
    sec=db.query(CourseSection).filter(CourseSection.id==it.section_id).first()
    c=db.query(Course).filter(Course.id==sec.course_id).first()
    if c.author_id!=u.id and u.role!="owner": raise HTTPException(403)
    if it.file_path and os.path.exists(it.file_path): os.remove(it.file_path)
    for sb in it.subblocks:
        if sb.file_path and os.path.exists(sb.file_path): os.remove(sb.file_path)
    db.delete(it); db.commit()

# ═══ COURSE ITEM MEDIA + SUBBLOCKS ═══
@app.post("/api/course-items/{iid}/upload",response_model=CourseSectionItemOut)
async def upload_citem_media(iid:str,file:UploadFile=File(...),u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    it=db.query(CourseSectionItem).options(joinedload(CourseSectionItem.subblocks)).filter(CourseSectionItem.id==iid).first()
    if not it: raise HTTPException(404)
    _rm(it.file_path)
    aid=gen_id(); ext=os.path.splitext(file.filename)[1] if file.filename else ""
    content=await file.read(); fp,dbp=_up(aid,ext)
    if len(content)>50*1024*1024: raise HTTPException(413)
    with open(fp,"wb") as f: f.write(content)
    it.file_path=dbp; it.mime=file.content_type or "application/octet-stream"; it.size=len(content)
    db.commit(); db.refresh(it); return it

@app.post("/api/course-items/{iid}/subblocks",response_model=CourseItemSubblockOut,status_code=201)
def add_course_subblock_text(iid:str,d:SubblockCreate,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    it=db.query(CourseSectionItem).filter(CourseSectionItem.id==iid).first()
    if not it: raise HTTPException(404)
    mp=db.query(CourseItemSubblock.position).filter(CourseItemSubblock.item_id==iid).order_by(CourseItemSubblock.position.desc()).first()
    sb=CourseItemSubblock(item_id=iid,type="text",content=d.content or "",position=(mp[0]+1) if mp else 0)
    db.add(sb); db.commit(); db.refresh(sb); return sb

@app.post("/api/course-items/{iid}/subblocks/media",response_model=CourseItemSubblockOut,status_code=201)
async def add_course_subblock_media(iid:str,file:UploadFile=File(...),u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    it=db.query(CourseSectionItem).filter(CourseSectionItem.id==iid).first()
    if not it: raise HTTPException(404)
    aid=gen_id(); ext=os.path.splitext(file.filename)[1] if file.filename else ""
    content=await file.read(); fp,dbp=_up(aid,ext)
    if len(content)>50*1024*1024: raise HTTPException(413)
    with open(fp,"wb") as f: f.write(content)
    mp=db.query(CourseItemSubblock.position).filter(CourseItemSubblock.item_id==iid).order_by(CourseItemSubblock.position.desc()).first()
    sb=CourseItemSubblock(item_id=iid,type="media",name=file.filename or "file",file_path=dbp,mime=file.content_type or "application/octet-stream",size=len(content),position=(mp[0]+1) if mp else 0)
    db.add(sb); db.commit(); db.refresh(sb); return sb

@app.patch("/api/course-subblocks/{sbid}",response_model=CourseItemSubblockOut)
def upd_course_subblock(sbid:str,d:SubblockCreate,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    sb=db.query(CourseItemSubblock).filter(CourseItemSubblock.id==sbid).first()
    if not sb: raise HTTPException(404)
    if d.content is not None: sb.content=d.content
    db.commit(); db.refresh(sb); return sb

@app.delete("/api/course-subblocks/{sbid}",status_code=204)
def del_course_subblock(sbid:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    sb=db.query(CourseItemSubblock).filter(CourseItemSubblock.id==sbid).first()
    if not sb: raise HTTPException(404)
    _rm(sb.file_path); db.delete(sb); db.commit()

# ═══ STUDENT ITEM SUBBLOCKS ═══
@app.post("/api/items/{iid}/subblocks",response_model=ItemSubblockOut,status_code=201)
def add_item_subblock_text(iid:str,d:SubblockCreate,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    it=db.query(Item).filter(Item.id==iid).first()
    if not it: raise HTTPException(404)
    sec=db.query(Section).filter(Section.id==it.section_id).first()
    if sec: chk_acc(sec.student_id,u,db)
    mp=db.query(ItemSubblock.position).filter(ItemSubblock.item_id==iid).order_by(ItemSubblock.position.desc()).first()
    sb=ItemSubblock(item_id=iid,type="text",content=d.content or "",position=(mp[0]+1) if mp else 0)
    db.add(sb); db.commit(); db.refresh(sb); return sb

@app.post("/api/items/{iid}/subblocks/media",response_model=ItemSubblockOut,status_code=201)
async def add_item_subblock_media(iid:str,file:UploadFile=File(...),u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    it=db.query(Item).filter(Item.id==iid).first()
    if not it: raise HTTPException(404)
    sec=db.query(Section).filter(Section.id==it.section_id).first()
    if sec: chk_acc(sec.student_id,u,db)
    aid=gen_id(); ext=os.path.splitext(file.filename)[1] if file.filename else ""
    content=await file.read(); fp,dbp=_up(aid,ext)
    if len(content)>50*1024*1024: raise HTTPException(413)
    with open(fp,"wb") as f: f.write(content)
    mp=db.query(ItemSubblock.position).filter(ItemSubblock.item_id==iid).order_by(ItemSubblock.position.desc()).first()
    sb=ItemSubblock(item_id=iid,type="media",name=file.filename or "file",file_path=dbp,mime=file.content_type or "application/octet-stream",size=len(content),position=(mp[0]+1) if mp else 0)
    db.add(sb); db.commit(); db.refresh(sb); return sb

@app.patch("/api/item-subblocks/{sbid}",response_model=ItemSubblockOut)
def upd_item_subblock(sbid:str,d:SubblockCreate,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    sb=db.query(ItemSubblock).filter(ItemSubblock.id==sbid).first()
    if not sb: raise HTTPException(404)
    if d.content is not None: sb.content=d.content
    db.commit(); db.refresh(sb); return sb

@app.delete("/api/item-subblocks/{sbid}",status_code=204)
def del_item_subblock(sbid:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    sb=db.query(ItemSubblock).filter(ItemSubblock.id==sbid).first()
    if not sb: raise HTTPException(404)
    _rm(sb.file_path); db.delete(sb); db.commit()

class CodeCheckRequest(BaseModel):
    code: str

@app.post("/api/items/{iid}/check-code")
def check_code_item(iid:str,d:CodeCheckRequest,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    it=db.query(Item).filter(Item.id==iid).first()
    if not it: raise HTTPException(404)
    if it.type!="code": raise HTTPException(400,"Не кодовый блок")
    if not it.note: raise HTTPException(400,"Нет эталонного ответа")
    def norm(s): return "\n".join(l.rstrip() for l in s.strip().splitlines())
    correct=norm(d.code)==norm(it.note)
    if correct:
        it.student_answer=d.code; it.status="done"; db.commit()
    return {"correct":correct}

# ═══ ACCESS CHECK ═══
def chk_acc(stid,u,db):
    if u.role=="owner": return
    if u.role=="teamlead":
        st=db.query(Student).filter(Student.id==stid).first()
        if not st: raise HTTPException(404)
        if stid in _teamlead_student_ids(u.id, db): return
        raise HTTPException(403,"Нет доступа")
    if u.role=="tutor":
        st=db.query(Student).filter(Student.id==stid).first()
        if not st: raise HTTPException(404)
        if st.created_by==u.id: return
        if not db.execute(tutor_student_link.select().where((tutor_student_link.c.tutor_id==u.id)&(tutor_student_link.c.student_id==stid))).first():
            raise HTTPException(403,"Нет доступа")
    elif u.role=="student":
        if u.student_id!=stid: raise HTTPException(403)
    elif u.role=="parent":
        if not db.execute(parent_student_link.select().where((parent_student_link.c.parent_id==u.id)&(parent_student_link.c.student_id==stid))).first():
            raise HTTPException(403)

# ═══ STUDENTS ═══
@app.get("/api/students",response_model=list[StudentListItem])
def list_students(u:User=Depends(get_current_user),tutor_id:str=None,teamlead_id:str=None,db:Session=Depends(get_db)):
    # is_demo: демо-пользователи видят только свои демо-данные, реальные — только is_demo=False
    _demo = getattr(u, "is_demo", False) or u.role in ("demo_tutor","demo_teamlead")
    if u.role=="owner":
        q = db.query(Student).filter(Student.is_demo==False)
        if tutor_id:
            lids=[r.student_id for r in db.execute(tutor_student_link.select().where(tutor_student_link.c.tutor_id==tutor_id)).fetchall()]
            q = q.filter(Student.id.in_(lids))
        if teamlead_id:
            tids=_team_tutor_ids(teamlead_id,db)
            if tids:
                sids=set()
                for tid in tids:
                    for r in db.execute(tutor_student_link.select().where(tutor_student_link.c.tutor_id==tid)).fetchall():
                        sids.add(r.student_id)
                q = q.filter(Student.id.in_(sids))
        return q.order_by(Student.created_at.desc()).all()
    if u.role=="teamlead":
        sids=_teamlead_student_ids(u.id,db)
        q = db.query(Student).filter(Student.id.in_(sids),Student.is_demo==False) if sids else db.query(Student).filter(False)
        if tutor_id: lids=[r.student_id for r in db.execute(tutor_student_link.select().where(tutor_student_link.c.tutor_id==tutor_id)).fetchall()]; q=q.filter(Student.id.in_(lids))
        return q.order_by(Student.created_at.desc()).all()
    if u.role in ("demo_tutor","demo_teamlead"):
        own=db.query(Student).filter(Student.created_by==u.id,Student.is_demo==True)
        lids=[r.student_id for r in db.execute(tutor_student_link.select().where(tutor_student_link.c.tutor_id==u.id)).fetchall()]
        return own.union(db.query(Student).filter(Student.id.in_(lids),Student.is_demo==True)).all() if lids else own.all()
    if u.role=="tutor":
        own=db.query(Student).filter(Student.created_by==u.id,Student.is_demo==False)
        lids=[r.student_id for r in db.execute(tutor_student_link.select().where(tutor_student_link.c.tutor_id==u.id)).fetchall()]
        return own.union(db.query(Student).filter(Student.id.in_(lids),Student.is_demo==False)).all() if lids else own.all()
    if u.role=="student":
        s=db.query(Student).filter(Student.id==u.student_id).first() if u.student_id else None
        return [s] if s else []
    if u.role=="parent":
        ids=[r.student_id for r in db.execute(parent_student_link.select().where(parent_student_link.c.parent_id==u.id)).fetchall()]
        return db.query(Student).filter(Student.id.in_(ids)).all() if ids else []
    return []

@app.get("/api/students/{stid}",response_model=StudentOut)
def get_student(stid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    chk_acc(stid,u,db)
    s=db.query(Student).options(
        joinedload(Student.sections).joinedload(Section.items).joinedload(Item.attachments),
        joinedload(Student.sections).joinedload(Section.items).joinedload(Item.subblocks)
    ).filter(Student.id==stid).first()
    if not s: raise HTTPException(404)
    return s

@app.get("/api/students/{stid}/contacts")
def get_contacts(stid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    chk_acc(stid,u,db)
    st=db.query(Student).filter(Student.id==stid).first()
    if not st: raise HTTPException(404)
    tutor_ids=set([r.tutor_id for r in db.execute(tutor_student_link.select().where(tutor_student_link.c.student_id==stid)).fetchall()])
    if st.created_by: tutor_ids.add(st.created_by)
    tutors=db.query(User).filter(User.id.in_(tutor_ids)).all() if tutor_ids else []
    parent_ids=[r.parent_id for r in db.execute(parent_student_link.select().where(parent_student_link.c.student_id==stid)).fetchall()]
    parents=db.query(User).filter(User.id.in_(parent_ids)).all() if parent_ids else []
    subj_map={s.id:s for s in db.query(Subject).all()}
    def tutor_subjects(t):
        sids=[r.subject_id for r in db.execute(tutor_subject_link.select().where(tutor_subject_link.c.tutor_id==t.id)).fetchall()]
        subjs=[subj_map[s].icon+" "+subj_map[s].name for s in sids if s in subj_map]
        if not subjs and t.subject_id and t.subject_id in subj_map:
            subjs=[subj_map[t.subject_id].icon+" "+subj_map[t.subject_id].name]
        return subjs
    return {
        "tutors":[{"id":t.id,"name":t.name,"subjects":tutor_subjects(t)} for t in tutors],
        "parents":[{"id":p.id,"name":p.name} for p in parents]
    }

@app.get("/api/students/{stid}/subjects")
def get_student_subjects(stid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    chk_acc(stid,u,db)
    rows=db.execute(_sa_text("SELECT ss.subject_id,s.name,s.icon FROM student_subjects ss JOIN subjects s ON s.id=ss.subject_id WHERE ss.student_id=:sid ORDER BY s.name").bindparams(sid=stid)).fetchall()
    return [{"subject_id":r[0],"name":r[1],"icon":r[2]} for r in rows]

@app.post("/api/students/{stid}/subjects",status_code=201)
def add_student_subject(stid:str,d:dict=Body(...),u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    chk_acc(stid,u,db)
    sid=d.get("subject_id","")
    if not db.query(Subject).filter(Subject.id==sid).first(): raise HTTPException(404,"Предмет не найден")
    db.execute(_sa_text("INSERT INTO student_subjects(student_id,subject_id) VALUES(:stid,:sid) ON CONFLICT DO NOTHING").bindparams(stid=stid,sid=sid))
    db.commit()
    return {"ok":True}

@app.delete("/api/students/{stid}/subjects/{sid}",status_code=200)
def del_student_subject(stid:str,sid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    chk_acc(stid,u,db)
    db.execute(_sa_text("DELETE FROM student_subjects WHERE student_id=:stid AND subject_id=:sid").bindparams(stid=stid,sid=sid))
    db.commit()
    return {"ok":True}

# ── Кейсы целей ученика (предмет + тип цели) ────────────────────────────────
VALID_GOALS=["ege","oge","olymp","improve_grades","deepening","extra_education"]

@app.get("/api/students/{stid}/goal-cases")
def get_goal_cases(stid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    chk_acc(stid,u,db)
    rows=db.execute(_sa_text(
        "SELECT gc.id,gc.subject_id,s.name,s.icon,gc.goal FROM student_goal_cases gc "
        "LEFT JOIN subjects s ON s.id=gc.subject_id WHERE gc.student_id=:sid ORDER BY gc.created_at"
    ).bindparams(sid=stid)).fetchall()
    return [{"id":r[0],"subject_id":r[1],"subject_name":r[2],"subject_icon":r[3],"goal":r[4]} for r in rows]

@app.post("/api/students/{stid}/goal-cases",status_code=201)
def add_goal_case(stid:str,d:dict=Body(...),u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    chk_acc(stid,u,db)
    goal=d.get("goal","ege")
    if goal not in VALID_GOALS: raise HTTPException(400,"Неверный тип цели")
    sid=d.get("subject_id") or None
    if sid and not db.query(Subject).filter(Subject.id==sid).first(): raise HTTPException(404,"Предмет не найден")
    # Проверка дубликата
    dup=db.execute(_sa_text(
        "SELECT 1 FROM student_goal_cases WHERE student_id=:stid AND goal=:goal AND (subject_id=:sid OR (subject_id IS NULL AND :sid IS NULL))"
    ).bindparams(stid=stid,goal=goal,sid=sid)).fetchone()
    if dup: raise HTTPException(409,"Такой кейс уже добавлен")
    cid=secrets.token_hex(6)
    db.execute(_sa_text(
        "INSERT INTO student_goal_cases(id,student_id,subject_id,goal) VALUES(:id,:stid,:sid,:goal)"
    ).bindparams(id=cid,stid=stid,sid=sid,goal=goal))
    db.commit()
    return {"id":cid,"ok":True}

@app.delete("/api/students/{stid}/goal-cases/{cid}",status_code=200)
def del_goal_case(stid:str,cid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    chk_acc(stid,u,db)
    db.execute(_sa_text("DELETE FROM student_goal_cases WHERE id=:cid AND student_id=:stid").bindparams(cid=cid,stid=stid))
    db.commit()
    return {"ok":True}

# ══ ОБМЕННИК ════════════════════════════════════════════════════════════════

# ── Скиллы тьютора ──────────────────────────────────────────────────────────
@app.get("/api/tutor/skill-cases")
def get_skill_cases(u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    if u.role not in ("tutor","owner","teamlead","demo_tutor","demo_teamlead"): raise HTTPException(403)
    rows=db.execute(_sa_text("SELECT tc.id,tc.subject_id,s.name,s.icon,tc.goal FROM tutor_skill_cases tc LEFT JOIN subjects s ON s.id=tc.subject_id WHERE tc.tutor_id=:tid ORDER BY tc.created_at").bindparams(tid=u.id)).fetchall()
    return [{"id":r[0],"subject_id":r[1],"subject_name":r[2],"subject_icon":r[3],"goal":r[4]} for r in rows]

@app.post("/api/tutor/skill-cases",status_code=201)
def add_skill_case(d:dict=Body(...),u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    if u.role not in ("tutor","owner","teamlead","demo_tutor","demo_teamlead"): raise HTTPException(403)
    goal=d.get("goal","ege")
    if goal not in VALID_GOALS: raise HTTPException(400,"Неверный тип")
    sid=d.get("subject_id") or None
    if sid and not db.query(Subject).filter(Subject.id==sid).first(): raise HTTPException(404,"Предмет не найден")
    dup=db.execute(_sa_text("SELECT 1 FROM tutor_skill_cases WHERE tutor_id=:tid AND goal=:goal AND (subject_id=:sid OR (subject_id IS NULL AND :sid IS NULL))").bindparams(tid=u.id,goal=goal,sid=sid)).fetchone()
    if dup: raise HTTPException(409,"Такой навык уже добавлен")
    cid=secrets.token_hex(6)
    db.execute(_sa_text("INSERT INTO tutor_skill_cases(id,tutor_id,subject_id,goal) VALUES(:id,:tid,:sid,:goal)").bindparams(id=cid,tid=u.id,sid=sid,goal=goal))
    db.commit()
    return {"id":cid,"ok":True}

@app.delete("/api/tutor/skill-cases/{cid}",status_code=200)
def del_skill_case(cid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    if u.role not in ("tutor","owner","teamlead","demo_tutor","demo_teamlead"): raise HTTPException(403)
    db.execute(_sa_text("DELETE FROM tutor_skill_cases WHERE id=:cid AND tutor_id=:tid").bindparams(cid=cid,tid=u.id))
    db.commit()
    return {"ok":True}

@app.patch("/api/tutor/recruiting")
def set_recruiting(d:dict=Body(...),u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    if u.role not in ("tutor","owner","teamlead","demo_tutor","demo_teamlead"): raise HTTPException(403)
    val=bool(d.get("is_recruiting",False))
    if val:
        cnt=db.execute(_sa_text("SELECT COUNT(*) FROM tutor_skill_cases WHERE tutor_id=:tid").bindparams(tid=u.id)).scalar()
        if not cnt:
            raise HTTPException(400,"Добавьте хотя бы один навык, чтобы ученики понимали, что вы преподаёте")
    db.execute(_sa_text("UPDATE users SET is_recruiting=:v WHERE id=:id").bindparams(v=val,id=u.id))
    db.commit()
    return {"ok":True,"is_recruiting":val}

# ── Флаг ученика "ищу преподавателя" ────────────────────────────────────────
@app.patch("/api/student/searching")
def set_searching(d:dict=Body(...),u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    if not u.student_id: raise HTTPException(404,"Профиль ученика не привязан")
    val=bool(d.get("is_searching",False))
    if val:
        cnt=db.execute(_sa_text("SELECT COUNT(*) FROM student_goal_cases WHERE student_id=:sid").bindparams(sid=u.student_id)).scalar()
        if not cnt:
            raise HTTPException(400,"Добавьте хотя бы одну цель, чтобы преподаватели понимали, что вам нужно")
    db.execute(_sa_text("UPDATE students SET is_searching=:v WHERE id=:id").bindparams(v=val,id=u.student_id))
    db.commit()
    return {"ok":True,"is_searching":val}

# ── Каталоги обменника ───────────────────────────────────────────────────────
def _subjects_set(rows):
    """Возвращает множество subject_id (без None) из строк с полем subject_id."""
    return {r[0] for r in rows if r[0] is not None}

def _has_any_null_subject(rows):
    """True если среди кейсов есть запись без предмета (= 'любой предмет')."""
    return any(r[0] is None for r in rows)

@app.get("/api/exchange/tutors")
def exchange_tutors(u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    # Предметы ученика из его goal_cases
    student_subjs=set()
    student_has_any=False
    if u.student_id:
        s_rows=db.execute(_sa_text(
            "SELECT subject_id FROM student_goal_cases WHERE student_id=:sid"
        ).bindparams(sid=u.student_id)).fetchall()
        student_subjs=_subjects_set(s_rows)
        student_has_any=_has_any_null_subject(s_rows)

    tutor_rows=db.execute(_sa_text(
        "SELECT u.id,u.name,u.photo,u.about FROM users u "
        "WHERE u.is_recruiting=TRUE AND u.is_active=TRUE AND u.role IN ('tutor','owner','teamlead') "
        "AND EXISTS (SELECT 1 FROM tutor_skill_cases tc WHERE tc.tutor_id=u.id)"
    )).fetchall()

    result=[]
    for r in tutor_rows:
        skills=db.execute(_sa_text(
            "SELECT tc.id,tc.subject_id,s.name,s.icon,tc.goal FROM tutor_skill_cases tc "
            "LEFT JOIN subjects s ON s.id=tc.subject_id WHERE tc.tutor_id=:tid ORDER BY tc.created_at"
        ).bindparams(tid=r[0])).fetchall()
        skill_list=[{"id":s[0],"subject_id":s[1],"subject_name":s[2],"subject_icon":s[3],"goal":s[4]} for s in skills]

        tutor_subjs=_subjects_set(skills)
        tutor_has_any=_has_any_null_subject(skills)

        # Фильтр по предметам: пропускаем если нет пересечения
        # Исключение: если у любой из сторон нет предметов или есть "любой предмет" — показываем
        if student_subjs and tutor_subjs and not student_has_any and not tutor_has_any:
            if not (student_subjs & tutor_subjs):
                continue

        result.append({"id":r[0],"name":r[1],"photo":r[2],"about":r[3],"skills":skill_list})
    return result

@app.get("/api/exchange/students")
def exchange_students(u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    # Предметы тьютора из его skill_cases
    t_rows=db.execute(_sa_text(
        "SELECT subject_id FROM tutor_skill_cases WHERE tutor_id=:tid"
    ).bindparams(tid=u.id)).fetchall()
    tutor_subjs=_subjects_set(t_rows)
    tutor_has_any=_has_any_null_subject(t_rows)

    student_rows=db.execute(_sa_text(
        "SELECT s.id,s.grade,s.format,s.level,u.id,u.name FROM students s "
        "JOIN users u ON u.student_id=s.id WHERE s.is_searching=TRUE AND u.is_active=TRUE "
        "AND EXISTS (SELECT 1 FROM student_goal_cases gc WHERE gc.student_id=s.id)"
    )).fetchall()

    result=[]
    for r in student_rows:
        goals=db.execute(_sa_text(
            "SELECT gc.id,gc.subject_id,sub.name,sub.icon,gc.goal FROM student_goal_cases gc "
            "LEFT JOIN subjects sub ON sub.id=gc.subject_id WHERE gc.student_id=:sid ORDER BY gc.created_at"
        ).bindparams(sid=r[0])).fetchall()
        goal_list=[{"id":g[0],"subject_id":g[1],"subject_name":g[2],"subject_icon":g[3],"goal":g[4]} for g in goals]

        student_subjs=_subjects_set(goals)
        student_has_any=_has_any_null_subject(goals)

        if tutor_subjs and student_subjs and not tutor_has_any and not student_has_any:
            if not (tutor_subjs & student_subjs):
                continue

        result.append({"student_id":r[0],"grade":r[1],"format":r[2],"level":r[3],"user_id":r[4],"name":r[5],"goal_cases":goal_list})
    return result

# ── Публичные профильные данные для просмотра чужого профиля ─────────────────
@app.get("/api/users/{uid}/skill-cases")
def get_user_skill_cases(uid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    rows=db.execute(_sa_text("SELECT tc.id,tc.subject_id,s.name,s.icon,tc.goal FROM tutor_skill_cases tc LEFT JOIN subjects s ON s.id=tc.subject_id WHERE tc.tutor_id=:tid ORDER BY tc.created_at").bindparams(tid=uid)).fetchall()
    return [{"id":r[0],"subject_id":r[1],"subject_name":r[2],"subject_icon":r[3],"goal":r[4]} for r in rows]

@app.get("/api/users/{uid}/student-info")
def get_user_student_info(uid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    target=db.query(User).filter(User.id==uid).first()
    if not target or not target.student_id: return {"grade":None,"goal_cases":[]}
    st=db.query(Student).filter(Student.id==target.student_id).first()
    goals=db.execute(_sa_text("SELECT gc.id,gc.subject_id,sub.name,sub.icon,gc.goal FROM student_goal_cases gc LEFT JOIN subjects sub ON sub.id=gc.subject_id WHERE gc.student_id=:sid ORDER BY gc.created_at").bindparams(sid=target.student_id)).fetchall()
    return {"grade":st.grade if st else None,"level":st.level if st else None,"format":st.format if st else None,"goal_cases":[{"id":g[0],"subject_id":g[1],"subject_name":g[2],"subject_icon":g[3],"goal":g[4]} for g in goals]}

# ══ КУРСЫ v2 ══════════════════════════════════════════════════════════════════
COURSE_STORAGE_LIMIT = 200 * 1024 * 1024  # 200 MB

def _course_access(cid:str, u:User, db:Session, write:bool=False):
    """Проверяет доступ к курсу. write=True — только автор/owner."""
    c = db.execute(_sa_text("SELECT id,author_id FROM courses_v2 WHERE id=:id").bindparams(id=cid)).fetchone()
    if not c: raise HTTPException(404, "Курс не найден")
    if write and u.role != "owner" and c[1] != u.id: raise HTTPException(403, "Только автор курса может редактировать")
    return c

def _enrollment_access(eid:str, u:User, db:Session):
    """Проверяет доступ к enrollment. Возвращает строку enrollment."""
    e = db.execute(_sa_text("SELECT id,course_id,student_id,tutor_id FROM course_enrollments WHERE id=:id").bindparams(id=eid)).fetchone()
    if not e: raise HTTPException(404, "Запись не найдена")
    # Студент — только свой
    if u.role == "student":
        if not u.student_id or u.student_id != e[2]: raise HTTPException(403)
    # Тьютор — только назначенный
    elif u.role in ("tutor","teamlead","demo_tutor","demo_teamlead"):
        if e[3] != u.id and u.role != "owner": raise HTTPException(403)
    return e

def _serialize_course(c, db:Session, with_lessons=False):
    modules = db.execute(_sa_text(
        "SELECT id,title,position,unlock_threshold,points_per_lesson FROM course_modules WHERE course_id=:cid ORDER BY position"
    ).bindparams(cid=c[0])).fetchall()
    lessons = db.execute(_sa_text(
        "SELECT id,title,estimated_min,position,module_id FROM course_lessons WHERE course_id=:cid ORDER BY position"
    ).bindparams(cid=c[0])).fetchall()
    checkpoints = db.execute(_sa_text(
        "SELECT id,after_position,title FROM course_checkpoints WHERE course_id=:cid ORDER BY after_position"
    ).bindparams(cid=c[0])).fetchall()
    result = {
        "id": c[0], "title": c[1], "description": c[2], "cover": c[3],
        "subject_id": c[4], "author_id": c[5], "is_published": c[6],
        "storage_bytes": c[7], "created_at": c[8].isoformat() if c[8] else None,
        "modules": [{"id":m[0],"title":m[1],"position":m[2],"unlock_threshold":m[3],"points_per_lesson":m[4]} for m in modules],
        "checkpoints": [{"id":cp[0],"after_position":cp[1],"title":cp[2]} for cp in checkpoints],
        "lessons": []
    }
    for l in lessons:
        lesson_data = {"id":l[0],"title":l[1],"estimated_min":l[2],"position":l[3],"module_id":l[4],"blocks":[]}
        if with_lessons:
            blocks = db.execute(_sa_text(
                "SELECT id,type,position,payload,file_path,file_name,file_mime,file_size FROM lesson_blocks WHERE lesson_id=:lid ORDER BY position"
            ).bindparams(lid=l[0])).fetchall()
            lesson_data["blocks"] = [{"id":b[0],"type":b[1],"position":b[2],"payload":b[3],"file_path":b[4],"file_name":b[5],"file_mime":b[6],"file_size":b[7]} for b in blocks]
        result["lessons"].append(lesson_data)
    return result

# ── CRUD курсов ──────────────────────────────────────────────────────────────
@app.get("/api/v2/courses")
def list_courses_v2(u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    # Все видят только: свои курсы любого статуса + чужие опубликованные
    rows = db.execute(_sa_text(
        "SELECT id,title,description,cover,subject_id,author_id,is_published,storage_bytes,created_at FROM courses_v2 "
        "WHERE author_id=:uid OR (is_published=TRUE AND author_id!=:uid) ORDER BY updated_at DESC"
    ).bindparams(uid=u.id)).fetchall()
    return [_serialize_course(c,db) for c in rows]

@app.post("/api/v2/courses",status_code=201)
def create_course_v2(d:dict=Body(...),u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    cid=secrets.token_hex(6)
    db.execute(_sa_text(
        "INSERT INTO courses_v2(id,title,description,subject_id,author_id) VALUES(:id,:title,:desc,:sid,:uid)"
    ).bindparams(id=cid,title=d.get("title","Новый курс"),desc=d.get("description",""),sid=d.get("subject_id"),uid=u.id))
    db.commit()
    row=db.execute(_sa_text("SELECT id,title,description,cover,subject_id,author_id,is_published,storage_bytes,created_at FROM courses_v2 WHERE id=:id").bindparams(id=cid)).fetchone()
    return _serialize_course(row,db)

@app.get("/api/v2/courses/{cid}")
def get_course_v2(cid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    row=db.execute(_sa_text("SELECT id,title,description,cover,subject_id,author_id,is_published,storage_bytes,created_at FROM courses_v2 WHERE id=:id").bindparams(id=cid)).fetchone()
    if not row: raise HTTPException(404)
    # Доступ: только автор курса или опубликованный курс
    if row[5]!=u.id and not row[6]: raise HTTPException(403,"Курс не опубликован")
    return _serialize_course(row,db,with_lessons=True)

@app.patch("/api/v2/courses/{cid}")
def update_course_v2(cid:str,d:dict=Body(...),u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    _course_access(cid,u,db,write=True)
    sets,params=[],{"id":cid}
    for f in ("title","description","cover","subject_id","is_published"):
        if f in d: sets.append(f"{'is_published' if f=='is_published' else f}=:{f}"); params[f]=d[f]
    if not sets: return {"ok":True}
    sets.append("updated_at=NOW()")
    db.execute(_sa_text(f"UPDATE courses_v2 SET {','.join(sets)} WHERE id=:id").bindparams(**params))
    db.commit()
    return {"ok":True}

@app.delete("/api/v2/courses/{cid}",status_code=204)
def delete_course_v2(cid:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    _course_access(cid,u,db,write=True)
    # Удаляем файлы (TODO: физически если нужно)
    db.execute(_sa_text("DELETE FROM courses_v2 WHERE id=:id").bindparams(id=cid))
    db.commit()

# ── УРОКИ ────────────────────────────────────────────────────────────────────
@app.post("/api/v2/courses/{cid}/lessons",status_code=201)
def add_lesson(cid:str,d:dict=Body(...),u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    _course_access(cid,u,db,write=True)
    max_pos=db.execute(_sa_text("SELECT COALESCE(MAX(position),0) FROM course_lessons WHERE course_id=:cid").bindparams(cid=cid)).scalar()
    lid=secrets.token_hex(6)
    pos=d.get("position", max_pos+1)
    db.execute(_sa_text(
        "INSERT INTO course_lessons(id,course_id,title,estimated_min,position) VALUES(:id,:cid,:title,:min,:pos)"
    ).bindparams(id=lid,cid=cid,title=d.get("title","Урок"),min=d.get("estimated_min",10),pos=pos))
    db.commit()
    return {"id":lid,"ok":True}

@app.patch("/api/v2/lessons/{lid}")
def update_lesson(lid:str,d:dict=Body(...),u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    row=db.execute(_sa_text("SELECT course_id FROM course_lessons WHERE id=:id").bindparams(id=lid)).fetchone()
    if not row: raise HTTPException(404)
    _course_access(row[0],u,db,write=True)
    sets,params=[],{"id":lid}
    for f in ("title","estimated_min","position","module_id"):
        if f in d:
            sets.append(f"{f}=:{f}")
            params[f]=d[f]  # module_id может быть None
    if sets:
        db.execute(_sa_text(f"UPDATE course_lessons SET {','.join(sets)} WHERE id=:id").bindparams(**params))
        db.commit()
    return {"ok":True}

# ── МОДУЛИ КУРСА ──────────────────────────────────────────────────────────────
@app.post("/api/v2/courses/{cid}/modules",status_code=201)
def add_module(cid:str,d:dict=Body(...),u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    _course_access(cid,u,db,write=True)
    max_pos=db.execute(_sa_text("SELECT COALESCE(MAX(position),0) FROM course_modules WHERE course_id=:cid").bindparams(cid=cid)).scalar()
    mid=secrets.token_hex(6)
    db.execute(_sa_text("INSERT INTO course_modules(id,course_id,title,position,unlock_threshold,points_per_lesson) VALUES(:id,:cid,:title,:pos,:thr,:ppl)").bindparams(id=mid,cid=cid,title=d.get("title","Модуль"),pos=d.get("position",max_pos+1),thr=d.get("unlock_threshold",0),ppl=d.get("points_per_lesson",1)))
    db.commit()
    return {"id":mid,"ok":True}

@app.patch("/api/v2/modules/{mid}")
def update_module(mid:str,d:dict=Body(...),u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    row=db.execute(_sa_text("SELECT course_id FROM course_modules WHERE id=:id").bindparams(id=mid)).fetchone()
    if not row: raise HTTPException(404)
    _course_access(row[0],u,db,write=True)
    sets,params=[],{"id":mid}
    for f in ("title","position","unlock_threshold","points_per_lesson"):
        if f in d: sets.append(f"{f}=:{f}"); params[f]=d[f]
    if sets: db.execute(_sa_text(f"UPDATE course_modules SET {','.join(sets)} WHERE id=:id").bindparams(**params)); db.commit()
    return {"ok":True}

@app.delete("/api/v2/modules/{mid}",status_code=204)
def delete_module(mid:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    row=db.execute(_sa_text("SELECT course_id FROM course_modules WHERE id=:id").bindparams(id=mid)).fetchone()
    if not row: raise HTTPException(404)
    _course_access(row[0],u,db,write=True)
    db.execute(_sa_text("UPDATE course_lessons SET module_id=NULL WHERE module_id=:mid").bindparams(mid=mid))
    db.execute(_sa_text("DELETE FROM course_modules WHERE id=:id").bindparams(id=mid))
    db.commit()

# ── Экспорт / импорт курса (.hbmcourse) ─────────────────────────────────────
import json as _json_mod
from fastapi.responses import Response as _FResponse

@app.get("/api/v2/courses/{cid}/export")
def export_course(cid:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    _course_access(cid,u,db)
    row=db.execute(_sa_text("SELECT id,title,description,cover,subject_id,author_id,is_published,storage_bytes,created_at FROM courses_v2 WHERE id=:id").bindparams(id=cid)).fetchone()
    if not row: raise HTTPException(404)
    course=_serialize_course(row,db,with_lessons=True)
    # Формируем портативный формат (без бинарных файлов)
    payload={
        "format":"hbmcourse","version":"1.0",
        "title":course["title"],"description":course["description"],
        "modules":[{"_export_id":m["id"],"title":m["title"],"position":m["position"],"unlock_threshold":m["unlock_threshold"],"points_per_lesson":m["points_per_lesson"]} for m in course["modules"]],
        "lessons":[{"_export_id":l["id"],"_module_export_id":l.get("module_id"),"title":l["title"],"estimated_min":l["estimated_min"],"position":l["position"]} for l in course["lessons"]],
        "blocks":[{"_export_id":b["id"],"_lesson_export_id":l["id"],"type":b["type"],"position":b["position"],"payload":b["payload"],"file_name":b.get("file_name")} for l in course["lessons"] for b in l.get("blocks",[]) if b["type"] not in ("image","file","video") or not b.get("file_path")]
    }
    # Добавляем текстовые блоки видео (URL-только)
    for l in course["lessons"]:
        for b in l.get("blocks",[]):
            if b["type"]=="video" and b["payload"]:
                payload["blocks"].append({"_export_id":b["id"],"_lesson_export_id":l["id"],"type":"video","position":b["position"],"payload":b["payload"],"file_name":None})
    fname=f"{course['title'] or 'course'}.hbmcourse".replace(" ","_")
    return _FResponse(content=_json_mod.dumps(payload,ensure_ascii=False,indent=2),media_type="application/json",headers={"Content-Disposition":f'attachment; filename="{fname}"'})

@app.post("/api/v2/courses/import",status_code=201)
async def import_course(file:UploadFile=File(...),u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    data=await file.read()
    try: payload=_json_mod.loads(data)
    except: raise HTTPException(400,"Неверный формат файла")
    if payload.get("format")!="hbmcourse": raise HTTPException(400,"Файл не является .hbmcourse")
    cid=secrets.token_hex(6)
    db.execute(_sa_text("INSERT INTO courses_v2(id,title,description,author_id) VALUES(:id,:title,:desc,:uid)").bindparams(id=cid,title=payload.get("title","Импортированный курс"),desc=payload.get("description",""),uid=u.id))
    # Модули
    mid_map={}
    for m in payload.get("modules",[]):
        new_mid=secrets.token_hex(6); mid_map[m["_export_id"]]=new_mid
        db.execute(_sa_text("INSERT INTO course_modules(id,course_id,title,position,unlock_threshold,points_per_lesson) VALUES(:id,:cid,:title,:pos,:thr,:ppl)").bindparams(id=new_mid,cid=cid,title=m.get("title","Модуль"),pos=m.get("position",0),thr=m.get("unlock_threshold",0),ppl=m.get("points_per_lesson",1)))
    # Уроки
    lid_map={}
    for l in payload.get("lessons",[]):
        new_lid=secrets.token_hex(6); lid_map[l["_export_id"]]=new_lid
        mid=mid_map.get(l.get("_module_export_id"))
        db.execute(_sa_text("INSERT INTO course_lessons(id,course_id,title,estimated_min,position,module_id) VALUES(:id,:cid,:title,:min,:pos,:mid)").bindparams(id=new_lid,cid=cid,title=l.get("title","Урок"),min=l.get("estimated_min",10),pos=l.get("position",0),mid=mid))
    # Блоки
    for b in payload.get("blocks",[]):
        new_bid=secrets.token_hex(6)
        new_lid=lid_map.get(b.get("_lesson_export_id"))
        if not new_lid: continue
        db.execute(_sa_text("INSERT INTO lesson_blocks(id,lesson_id,type,position,payload) VALUES(:id,:lid,:type,:pos,CAST(:payload AS jsonb))").bindparams(id=new_bid,lid=new_lid,type=b.get("type","text"),pos=b.get("position",0),payload=_json_mod.dumps(b.get("payload") or {})))
    db.commit()
    return {"id":cid,"ok":True}

@app.delete("/api/v2/lessons/{lid}",status_code=204)
def delete_lesson(lid:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    row=db.execute(_sa_text("SELECT course_id FROM course_lessons WHERE id=:id").bindparams(id=lid)).fetchone()
    if not row: raise HTTPException(404)
    _course_access(row[0],u,db,write=True)
    db.execute(_sa_text("DELETE FROM course_lessons WHERE id=:id").bindparams(id=lid))
    db.commit()

@app.post("/api/v2/courses/{cid}/lessons/reorder")
def reorder_lessons(cid:str,d:dict=Body(...),u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    _course_access(cid,u,db,write=True)
    order=d.get("order",[]) # list of {id, position}
    for item in order:
        db.execute(_sa_text("UPDATE course_lessons SET position=:pos WHERE id=:id AND course_id=:cid").bindparams(pos=item["position"],id=item["id"],cid=cid))
    db.commit()
    return {"ok":True}

# ── БЛОКИ УРОКА ──────────────────────────────────────────────────────────────
@app.post("/api/v2/lessons/{lid}/blocks",status_code=201)
def add_block(lid:str,d:dict=Body(...),u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    row=db.execute(_sa_text("SELECT course_id FROM course_lessons WHERE id=:id").bindparams(id=lid)).fetchone()
    if not row: raise HTTPException(404)
    _course_access(row[0],u,db,write=True)
    max_pos=db.execute(_sa_text("SELECT COALESCE(MAX(position),0) FROM lesson_blocks WHERE lesson_id=:lid").bindparams(lid=lid)).scalar()
    bid=secrets.token_hex(6)
    import json as _json
    payload=_json.dumps(d.get("payload",{}))
    db.execute(_sa_text(
        "INSERT INTO lesson_blocks(id,lesson_id,type,position,payload) VALUES(:id,:lid,:type,:pos,CAST(:payload AS jsonb))"
    ).bindparams(id=bid,lid=lid,type=d.get("type","text"),pos=d.get("position",max_pos+1),payload=payload))
    db.commit()
    return {"id":bid,"ok":True}

@app.patch("/api/v2/lesson-blocks/{bid}")
def update_block(bid:str,d:dict=Body(...),u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    row=db.execute(_sa_text("SELECT lb.lesson_id,cl.course_id FROM lesson_blocks lb JOIN course_lessons cl ON cl.id=lb.lesson_id WHERE lb.id=:id").bindparams(id=bid)).fetchone()
    if not row: raise HTTPException(404)
    _course_access(row[1],u,db,write=True)
    import json as _json
    if "payload" in d:
        db.execute(_sa_text("UPDATE lesson_blocks SET payload=CAST(:p AS jsonb) WHERE id=:id").bindparams(p=_json.dumps(d["payload"]),id=bid))
    if "position" in d:
        db.execute(_sa_text("UPDATE lesson_blocks SET position=:pos WHERE id=:id").bindparams(pos=d["position"],id=bid))
    db.commit()
    return {"ok":True}

@app.delete("/api/v2/lesson-blocks/{bid}",status_code=204)
def delete_block(bid:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    row=db.execute(_sa_text("SELECT lb.lesson_id,cl.course_id,lb.file_size,lb.file_path FROM lesson_blocks lb JOIN course_lessons cl ON cl.id=lb.lesson_id WHERE lb.id=:id").bindparams(id=bid)).fetchone()
    if not row: raise HTTPException(404)
    _course_access(row[1],u,db,write=True)
    if row[2]: db.execute(_sa_text("UPDATE courses_v2 SET storage_bytes=GREATEST(0,storage_bytes-:s) WHERE id=:cid").bindparams(s=row[2],cid=row[1]))
    db.execute(_sa_text("DELETE FROM lesson_blocks WHERE id=:id").bindparams(id=bid))
    db.commit()

@app.post("/api/v2/lesson-blocks/{bid}/upload")
async def upload_block_file(bid:str,file:UploadFile=File(...),u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    row=db.execute(_sa_text("SELECT lb.lesson_id,cl.course_id,lb.file_size FROM lesson_blocks lb JOIN course_lessons cl ON cl.id=lb.lesson_id WHERE lb.id=:id").bindparams(id=bid)).fetchone()
    if not row: raise HTTPException(404)
    _course_access(row[1],u,db,write=True)
    course_storage=db.execute(_sa_text("SELECT storage_bytes FROM courses_v2 WHERE id=:id").bindparams(id=row[1])).scalar() or 0
    old_size=row[2] or 0
    data=await file.read()
    new_size=len(data)
    if course_storage - old_size + new_size > COURSE_STORAGE_LIMIT:
        raise HTTPException(413,f"Превышен лимит хранилища курса 200 МБ. Используется: {(course_storage-old_size)/1024/1024:.1f} МБ, загружается: {new_size/1024/1024:.1f} МБ")
    path=f"uploads/courses/{row[1]}/{bid}_{file.filename}"
    os.makedirs(os.path.dirname(f"/opt/hbm/{path}"),exist_ok=True)
    with open(f"/opt/hbm/{path}","wb") as f_: f_.write(data)
    db.execute(_sa_text("UPDATE lesson_blocks SET file_path=:p,file_name=:n,file_mime=:m,file_size=:s WHERE id=:id").bindparams(p=path,n=file.filename,m=file.content_type or "application/octet-stream",s=new_size,id=bid))
    db.execute(_sa_text("UPDATE courses_v2 SET storage_bytes=storage_bytes-:old+:new,updated_at=NOW() WHERE id=:cid").bindparams(old=old_size,new=new_size,cid=row[1]))
    db.commit()
    return {"ok":True,"path":"/"+path,"size":new_size}

# ── РУБЕЖИ ───────────────────────────────────────────────────────────────────
@app.post("/api/v2/courses/{cid}/checkpoints",status_code=201)
def add_checkpoint(cid:str,d:dict=Body(...),u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    _course_access(cid,u,db,write=True)
    cpid=secrets.token_hex(6)
    db.execute(_sa_text("INSERT INTO course_checkpoints(id,course_id,after_position,title) VALUES(:id,:cid,:pos,:title)").bindparams(id=cpid,cid=cid,pos=d.get("after_position",0),title=d.get("title","")))
    db.commit()
    return {"id":cpid,"ok":True}

@app.patch("/api/v2/checkpoints/{cpid}")
def update_checkpoint(cpid:str,d:dict=Body(...),u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    row=db.execute(_sa_text("SELECT course_id FROM course_checkpoints WHERE id=:id").bindparams(id=cpid)).fetchone()
    if not row: raise HTTPException(404)
    _course_access(row[0],u,db,write=True)
    sets,params=[],{"id":cpid}
    if "after_position" in d: sets.append("after_position=:after_position"); params["after_position"]=d["after_position"]
    if "title" in d: sets.append("title=:title"); params["title"]=d["title"]
    if sets: db.execute(_sa_text(f"UPDATE course_checkpoints SET {','.join(sets)} WHERE id=:id").bindparams(**params)); db.commit()
    return {"ok":True}

@app.delete("/api/v2/checkpoints/{cpid}",status_code=204)
def delete_checkpoint(cpid:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    row=db.execute(_sa_text("SELECT course_id FROM course_checkpoints WHERE id=:id").bindparams(id=cpid)).fetchone()
    if not row: raise HTTPException(404)
    _course_access(row[0],u,db,write=True)
    db.execute(_sa_text("DELETE FROM course_checkpoints WHERE id=:id").bindparams(id=cpid)); db.commit()

# ── ЗАПИСИ НА КУРС (ENROLLMENT) ──────────────────────────────────────────────
@app.post("/api/v2/courses/{cid}/enroll",status_code=201)
def enroll_student(cid:str,d:dict=Body(...),u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    row=db.execute(_sa_text("SELECT id,is_published FROM courses_v2 WHERE id=:id").bindparams(id=cid)).fetchone()
    if not row: raise HTTPException(404)
    sid=d.get("student_id")
    if not sid: raise HTTPException(400,"student_id обязателен")
    eid=secrets.token_hex(6)
    db.execute(_sa_text(
        "INSERT INTO course_enrollments(id,course_id,student_id,tutor_id) VALUES(:id,:cid,:sid,:tid) ON CONFLICT(course_id,student_id) DO NOTHING"
    ).bindparams(id=eid,cid=cid,sid=sid,tid=u.id))
    db.commit()
    actual=db.execute(_sa_text("SELECT id FROM course_enrollments WHERE course_id=:cid AND student_id=:sid").bindparams(cid=cid,sid=sid)).fetchone()
    return {"id":actual[0] if actual else eid,"ok":True}

@app.get("/api/v2/enrollments")
def list_enrollments(u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    if u.role=="student" and u.student_id:
        rows=db.execute(_sa_text("SELECT e.id,e.course_id,c.title,c.cover,c.subject_id,e.enrolled_at FROM course_enrollments e JOIN courses_v2 c ON c.id=e.course_id WHERE e.student_id=:sid ORDER BY e.enrolled_at DESC").bindparams(sid=u.student_id)).fetchall()
    elif u.role in ("owner",):
        rows=db.execute(_sa_text("SELECT e.id,e.course_id,c.title,c.cover,c.subject_id,e.enrolled_at FROM course_enrollments e JOIN courses_v2 c ON c.id=e.course_id ORDER BY e.enrolled_at DESC")).fetchall()
    else:
        rows=db.execute(_sa_text("SELECT e.id,e.course_id,c.title,c.cover,c.subject_id,e.enrolled_at FROM course_enrollments e JOIN courses_v2 c ON c.id=e.course_id WHERE e.tutor_id=:tid ORDER BY e.enrolled_at DESC").bindparams(tid=u.id)).fetchall()
    return [{"id":r[0],"course_id":r[1],"course_title":r[2],"cover":r[3],"subject_id":r[4],"enrolled_at":r[5].isoformat() if r[5] else None} for r in rows]

@app.get("/api/v2/enrollments/{eid}")
def get_enrollment(eid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    e=_enrollment_access(eid,u,db)
    course_row=db.execute(_sa_text("SELECT id,title,description,cover,subject_id,author_id,is_published,storage_bytes,created_at FROM courses_v2 WHERE id=:id").bindparams(id=e[1])).fetchone()
    course=_serialize_course(course_row,db,with_lessons=True)
    progress=db.execute(_sa_text("SELECT lesson_id,done FROM lesson_progress WHERE enrollment_id=:eid").bindparams(eid=eid)).fetchall()
    done_set={r[0] for r in progress if r[1]}

    # ── Система баллов ───────────────────────────────────────────────────────
    # Строим словарь: lesson_id → points_per_lesson его модуля
    modules_map={m["id"]:m for m in course["modules"]}
    lesson_module={l["id"]:l.get("module_id") for l in course["lessons"]}

    # Считаем заработанные баллы
    total_points=0
    for lid in done_set:
        mid=lesson_module.get(lid)
        if mid and mid in modules_map:
            total_points+=modules_map[mid]["points_per_lesson"]
        else:
            total_points+=1  # уроки без модуля дают 1 балл

    # Определяем доступность каждого урока по баллам
    sorted_modules=sorted(course["modules"],key=lambda m:m["position"])
    unlocked_module_ids=set()
    for m in sorted_modules:
        if total_points>=m["unlock_threshold"]:
            unlocked_module_ids.add(m["id"])

    for lesson in course["lessons"]:
        lesson["done"]=lesson["id"] in done_set
        mid=lesson.get("module_id")
        if not mid:
            lesson["accessible"]=True  # без модуля — всегда доступен
        else:
            lesson["accessible"]=mid in unlocked_module_ids

    course["total_points"]=total_points
    course["enrollment_id"]=eid
    return course

def _max_accessible(lessons,checkpoints,unlocked_set):
    """Возвращает максимальный position доступного урока."""
    if not checkpoints: return 10**9
    sorted_cp=sorted(checkpoints,key=lambda x:x["after_position"])
    max_pos=10**9
    for cp in sorted_cp:
        if cp["id"] not in unlocked_set:
            max_pos=cp["after_position"]
            break
    return max_pos

# ── РАЗБЛОКИРОВКА РУБЕЖЕЙ (тьютор) ──────────────────────────────────────────
@app.post("/api/v2/enrollments/{eid}/unlock/{cpid}")
def unlock_checkpoint(eid:str,cpid:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    e=_enrollment_access(eid,u,db)
    cp=db.execute(_sa_text("SELECT id,course_id FROM course_checkpoints WHERE id=:id").bindparams(id=cpid)).fetchone()
    if not cp or cp[1]!=e[1]: raise HTTPException(404,"Рубеж не найден в этом курсе")
    db.execute(_sa_text(
        "INSERT INTO checkpoint_unlocks(enrollment_id,checkpoint_id,unlocked_by) VALUES(:eid,:cpid,:uid) ON CONFLICT DO NOTHING"
    ).bindparams(eid=eid,cpid=cpid,uid=u.id))
    db.commit()
    return {"ok":True}

@app.delete("/api/v2/enrollments/{eid}/unlock/{cpid}")
def lock_checkpoint(eid:str,cpid:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    _enrollment_access(eid,u,db)
    db.execute(_sa_text("DELETE FROM checkpoint_unlocks WHERE enrollment_id=:eid AND checkpoint_id=:cpid").bindparams(eid=eid,cpid=cpid))
    db.commit()
    return {"ok":True}

# ── ПРОГРЕСС СТУДЕНТА ─────────────────────────────────────────────────────────
@app.post("/api/v2/enrollments/{eid}/lessons/{lid}/complete")
def complete_lesson(eid:str,lid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    e=_enrollment_access(eid,u,db)
    # Проверяем доступность урока
    lesson=db.execute(_sa_text("SELECT position FROM course_lessons WHERE id=:id AND course_id=:cid").bindparams(id=lid,cid=e[1])).fetchone()
    if not lesson: raise HTTPException(404)
    unlocked=db.execute(_sa_text("SELECT checkpoint_id FROM checkpoint_unlocks WHERE enrollment_id=:eid").bindparams(eid=eid)).fetchall()
    unlocked_set={r[0] for r in unlocked}
    checkpoints=db.execute(_sa_text("SELECT id,after_position FROM course_checkpoints WHERE course_id=:cid").bindparams(cid=e[1])).fetchall()
    max_pos=_max_accessible([],checkpoints,unlocked_set) if not checkpoints else _max_accessible([],checkpoints,unlocked_set)
    # Упрощённая проверка: достаточно того, что урок доступен
    sorted_cp=sorted(checkpoints,key=lambda x:x[1])
    max_pos=10**9
    for cp in sorted_cp:
        if cp[0] not in unlocked_set: max_pos=cp[1]; break
    if lesson[0]>max_pos: raise HTTPException(403,"Урок недоступен — рубеж не открыт")
    from datetime import timezone as _tz
    db.execute(_sa_text(
        "INSERT INTO lesson_progress(enrollment_id,lesson_id,done,done_at) VALUES(:eid,:lid,TRUE,NOW()) ON CONFLICT(enrollment_id,lesson_id) DO UPDATE SET done=TRUE,done_at=NOW()"
    ).bindparams(eid=eid,lid=lid))
    db.commit()
    return {"ok":True}

@app.post("/api/v2/enrollments/{eid}/lessons/{lid}/quiz")
def submit_quiz(eid:str,lid:str,d:dict=Body(...),u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    import json as _json
    _enrollment_access(eid,u,db)
    block_id=d.get("block_id")
    answers=d.get("answers",[]) # [{question_id, answer}]
    results=[]
    if block_id:
        block=db.execute(_sa_text("SELECT payload FROM lesson_blocks WHERE id=:id").bindparams(id=block_id)).fetchone()
        questions={q["id"]:q for q in (block[0] or {}).get("questions",[])} if block else {}
        for ans in answers:
            qid=ans.get("question_id"); user_ans=ans.get("answer")
            q=questions.get(qid,{})
            correct=None
            if q.get("type")=="single":
                correct_opt=next((o["id"] for o in q.get("options",[]) if o.get("correct")),None)
                correct=(user_ans==correct_opt)
            elif q.get("type")=="multiple":
                correct_opts=set(o["id"] for o in q.get("options",[]) if o.get("correct"))
                correct=(set(user_ans or [])==correct_opts)
            qans_id=secrets.token_hex(6)
            db.execute(_sa_text(
                "INSERT INTO quiz_answers(id,enrollment_id,block_id,question_id,answer,is_correct) VALUES(:id,:eid,:bid,:qid,CAST(:ans AS jsonb),:correct) ON CONFLICT(enrollment_id,block_id,question_id) DO UPDATE SET answer=CAST(:ans AS jsonb),is_correct=:correct,answered_at=NOW()"
            ).bindparams(id=qans_id,eid=eid,bid=block_id,qid=qid,ans=_json.dumps(user_ans),correct=correct))
            results.append({"question_id":qid,"is_correct":correct})
    db.commit()
    return {"ok":True,"results":results}

# ── Обложка курса ─────────────────────────────────────────────────────────────
@app.post("/api/v2/courses/{cid}/cover")
async def upload_cover(cid:str,file:UploadFile=File(...),u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    _course_access(cid,u,db,write=True)
    data=await file.read()
    if len(data)>5*1024*1024: raise HTTPException(413,"Обложка не более 5 МБ")
    path=f"uploads/courses/{cid}/cover_{file.filename}"
    os.makedirs(os.path.dirname(f"/opt/hbm/{path}"),exist_ok=True)
    with open(f"/opt/hbm/{path}","wb") as f_: f_.write(data)
    db.execute(_sa_text("UPDATE courses_v2 SET cover=:p,updated_at=NOW() WHERE id=:id").bindparams(p="/"+path,id=cid))
    db.commit()
    return {"ok":True,"cover":"/"+path}

# ── Список рубежей с прогрессом для тьютора ──────────────────────────────────
@app.get("/api/v2/students/{stid}/enrollments")
def student_enrollments(stid:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    chk_acc(stid,u,db)
    rows=db.execute(_sa_text(
        "SELECT e.id,e.course_id,c.title,c.cover,e.enrolled_at FROM course_enrollments e JOIN courses_v2 c ON c.id=e.course_id WHERE e.student_id=:sid ORDER BY e.enrolled_at DESC"
    ).bindparams(sid=stid)).fetchall()
    result=[]
    for r in rows:
        eid=r[0]; cid=r[1]
        total=db.execute(_sa_text("SELECT COUNT(*) FROM course_lessons WHERE course_id=:cid").bindparams(cid=cid)).scalar() or 0
        done=db.execute(_sa_text("SELECT COUNT(*) FROM lesson_progress WHERE enrollment_id=:eid AND done=TRUE").bindparams(eid=eid)).scalar() or 0
        checkpoints=db.execute(_sa_text("SELECT cp.id,cp.after_position,cp.title,cu.unlocked_at FROM course_checkpoints cp LEFT JOIN checkpoint_unlocks cu ON cu.checkpoint_id=cp.id AND cu.enrollment_id=:eid WHERE cp.course_id=:cid ORDER BY cp.after_position").bindparams(eid=eid,cid=cid)).fetchall()
        result.append({"enrollment_id":eid,"course_id":cid,"course_title":r[2],"cover":r[3],"enrolled_at":r[4].isoformat() if r[4] else None,"total_lessons":total,"done_lessons":done,"checkpoints":[{"id":cp[0],"after_position":cp[1],"title":cp[2],"unlocked":cp[3] is not None} for cp in checkpoints]})
    return result

# ══ БЛОГ/ПОСТЫ ПРОФИЛЯ ═══════════════════════════════════════════════════════
@app.get("/api/users/{uid}/posts")
def get_user_posts(uid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    posts=db.execute(_sa_text("SELECT p.id,p.content,p.created_at,au.id,au.name,au.photo FROM profile_posts p JOIN users au ON au.id=p.author_id WHERE p.author_id=:uid ORDER BY p.created_at DESC").bindparams(uid=uid)).fetchall()
    result=[]
    for p in posts:
        comments=db.execute(_sa_text("SELECT c.id,c.content,c.created_at,cu.id,cu.name FROM post_comments c JOIN users cu ON cu.id=c.author_id WHERE c.post_id=:pid ORDER BY c.created_at").bindparams(pid=p[0])).fetchall()
        rxn_rows=db.execute(_sa_text("SELECT emoji,COUNT(*),bool_or(user_id=:me) FROM post_reactions WHERE post_id=:pid GROUP BY emoji").bindparams(pid=p[0],me=u.id)).fetchall()
        reactions={r[0]:{"count":int(r[1]),"mine":bool(r[2])} for r in rxn_rows}
        result.append({"id":p[0],"content":p[1],"created_at":p[2].isoformat() if p[2] else None,"author_id":p[3],"author_name":p[4],"author_photo":p[5],"reactions":reactions,"comments":[{"id":c[0],"content":c[1],"created_at":c[2].isoformat() if c[2] else None,"author_id":c[3],"author_name":c[4]} for c in comments]})
    return result

@app.post("/api/profile/posts/{pid}/reactions",status_code=200)
def toggle_reaction(pid:str,d:dict=Body(...),u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    if not db.execute(_sa_text("SELECT 1 FROM profile_posts WHERE id=:pid").bindparams(pid=pid)).fetchone(): raise HTTPException(404)
    emoji=d.get("emoji","❤️")
    if len(emoji)>10: raise HTTPException(400)
    existing=db.execute(_sa_text("SELECT 1 FROM post_reactions WHERE post_id=:pid AND user_id=:uid AND emoji=:emoji").bindparams(pid=pid,uid=u.id,emoji=emoji)).fetchone()
    if existing:
        db.execute(_sa_text("DELETE FROM post_reactions WHERE post_id=:pid AND user_id=:uid AND emoji=:emoji").bindparams(pid=pid,uid=u.id,emoji=emoji))
        action="removed"
    else:
        db.execute(_sa_text("INSERT INTO post_reactions(post_id,user_id,emoji) VALUES(:pid,:uid,:emoji) ON CONFLICT DO NOTHING").bindparams(pid=pid,uid=u.id,emoji=emoji))
        action="added"
    db.commit()
    rxn_rows=db.execute(_sa_text("SELECT emoji,COUNT(*),bool_or(user_id=:me) FROM post_reactions WHERE post_id=:pid GROUP BY emoji").bindparams(pid=pid,me=u.id)).fetchall()
    return {"ok":True,"action":action,"reactions":{r[0]:{"count":int(r[1]),"mine":bool(r[2])} for r in rxn_rows}}

@app.post("/api/profile/posts",status_code=201)
def create_post(d:dict=Body(...),u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    content=(d.get("content","")).strip()
    if not content or len(content)>5000: raise HTTPException(400,"Текст от 1 до 5000 символов")
    pid=secrets.token_hex(6)
    db.execute(_sa_text("INSERT INTO profile_posts(id,author_id,content) VALUES(:id,:uid,:content)").bindparams(id=pid,uid=u.id,content=content))
    db.commit()
    return {"id":pid,"ok":True}

@app.delete("/api/profile/posts/{pid}",status_code=200)
def delete_post(pid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    p=db.execute(_sa_text("SELECT author_id FROM profile_posts WHERE id=:pid").bindparams(pid=pid)).fetchone()
    if not p: raise HTTPException(404)
    if p[0]!=u.id and u.role!="owner": raise HTTPException(403)
    db.execute(_sa_text("DELETE FROM profile_posts WHERE id=:pid").bindparams(pid=pid))
    db.commit()
    return {"ok":True}

@app.post("/api/profile/posts/{pid}/comments",status_code=201)
def add_comment(pid:str,d:dict=Body(...),u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    if not db.execute(_sa_text("SELECT 1 FROM profile_posts WHERE id=:pid").bindparams(pid=pid)).fetchone(): raise HTTPException(404)
    content=(d.get("content","")).strip()
    if not content or len(content)>1000: raise HTTPException(400,"Комментарий от 1 до 1000 символов")
    cid=secrets.token_hex(6)
    db.execute(_sa_text("INSERT INTO post_comments(id,post_id,author_id,content) VALUES(:id,:pid,:uid,:content)").bindparams(id=cid,pid=pid,uid=u.id,content=content))
    db.commit()
    return {"id":cid,"ok":True}

@app.delete("/api/profile/posts/{pid}/comments/{cid}",status_code=200)
def delete_comment(pid:str,cid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    c=db.execute(_sa_text("SELECT author_id FROM post_comments WHERE id=:cid AND post_id=:pid").bindparams(cid=cid,pid=pid)).fetchone()
    if not c: raise HTTPException(404)
    if c[0]!=u.id and u.role!="owner": raise HTTPException(403)
    db.execute(_sa_text("DELETE FROM post_comments WHERE id=:cid").bindparams(cid=cid))
    db.commit()
    return {"ok":True}

@app.get("/api/students/{stid}/groups")
def get_student_groups(stid:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    chk_acc(stid,u,db)
    memberships=db.query(GroupMembership).filter(GroupMembership.student_id==stid,GroupMembership.left_at==None).all()
    result=[]
    for m in memberships:
        g=db.query(Group).filter(Group.id==m.group_id).first()
        if not g: continue
        tutor=db.query(User).filter(User.id==g.tutor_id).first()
        result.append({"membership_id":m.id,"group_id":g.id,"group_name":g.name,"tutor_id":g.tutor_id,"tutor_name":tutor.name if tutor else "?","joined_at":m.joined_at.isoformat()})
    return result

@app.post("/api/students",response_model=StudentOut,status_code=201)
def create_student(d:StudentCreate,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    s=Student(name=d.name,level=d.level,grade=d.grade,goal=d.goal,base_rate=d.base_rate,format=d.format,subject_id=d.subject_id,created_by=u.id)
    db.add(s); db.flush()
    if u.role=="tutor": db.execute(tutor_student_link.insert().values(tutor_id=u.id,student_id=s.id))
    tutor_id=u.id if u.role in ("tutor","teamlead") else None
    ci=CourseInstance(id=gen_id(),title="Основная программа",tutor_id=tutor_id,subject_id=d.subject_id,grade=d.grade,goal=d.goal)
    db.add(ci); db.flush()
    db.add(Enrollment(id=gen_id(),instance_id=ci.id,student_id=s.id))
    db.commit(); db.refresh(s)
    try: _sync_auto_group(s.id,db)
    except: pass
    return s

@app.patch("/api/students/{stid}",response_model=StudentOut)
def upd_student(stid:str,d:StudentUpdate,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    chk_acc(stid,u,db); s=db.query(Student).filter(Student.id==stid).first()
    if not s: raise HTTPException(404)
    for f,v in d.model_dump(exclude_unset=True).items(): setattr(s,f,v)
    db.commit(); db.refresh(s); return s

@app.delete("/api/students/{stid}",status_code=204)
def del_student(stid:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    chk_acc(stid,u,db); s=db.query(Student).filter(Student.id==stid).first()
    if not s: raise HTTPException(404)
    _cln_stu(s,db)
    db.query(User).filter(User.student_id==stid).delete(synchronize_session=False)
    db.flush()
    db.delete(s); db.commit()

@app.post("/api/students/{stid}/assign-tutor/{tid}",status_code=200)
def assign_tutor(stid:str,tid:str,o:User=Depends(require_owner),db:Session=Depends(get_db)):
    if not db.query(Student).filter(Student.id==stid).first(): raise HTTPException(404)
    if not db.query(User).filter(User.id==tid,User.role=="tutor").first(): raise HTTPException(404)
    if not db.execute(tutor_student_link.select().where((tutor_student_link.c.tutor_id==tid)&(tutor_student_link.c.student_id==stid))).first():
        db.execute(tutor_student_link.insert().values(tutor_id=tid,student_id=stid)); db.commit()
    try: _sync_auto_group(stid,db)
    except: pass
    return {"ok":True}

@app.delete("/api/students/{stid}/unassign-tutor/{tid}",status_code=200)
def unassign_tutor(stid:str,tid:str,o:User=Depends(require_owner),db:Session=Depends(get_db)):
    db.execute(tutor_student_link.delete().where((tutor_student_link.c.tutor_id==tid)&(tutor_student_link.c.student_id==stid))); db.commit()
    return {"ok":True}

@app.post("/api/students/{stid}/link-parent/{uid}",status_code=200)
def link_parent(stid:str,uid:str,o:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    chk_acc(stid,o,db)
    if not db.query(Student).filter(Student.id==stid).first(): raise HTTPException(404,"Student not found")
    if not db.query(User).filter(User.id==uid,User.role=="parent").first(): raise HTTPException(404,"Parent user not found")
    if not db.execute(parent_student_link.select().where((parent_student_link.c.parent_id==uid)&(parent_student_link.c.student_id==stid))).first():
        db.execute(parent_student_link.insert().values(parent_id=uid,student_id=stid)); db.commit()
    try: _sync_auto_group(stid,db)
    except: pass
    return {"ok":True}

# Apply course
@app.post("/api/students/{stid}/apply-course/{cid}",response_model=StudentOut)
def apply_course(stid:str,cid:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    chk_acc(stid,u,db); st=db.query(Student).filter(Student.id==stid).first()
    if not st: raise HTTPException(404)
    co=db.query(Course).options(
        joinedload(Course.sections).joinedload(CourseSection.items).joinedload(CourseSectionItem.subblocks)
    ).filter(Course.id==cid).first()
    if not co: raise HTTPException(404)
    if co.access=="private" and co.author_id!=u.id and u.role!="owner": raise HTTPException(403)
    for sec in st.sections: _cln_sec(sec,db)
    db.query(Section).filter(Section.student_id==stid).delete()
    for csec in co.sections:
        s=Section(student_id=stid,title=csec.title,position=csec.position,idz_enabled=csec.idz_enabled,control_enabled=csec.control_enabled,idz_text=csec.idz_text)
        db.add(s); db.flush()
        for ci in csec.items:
            ni=Item(section_id=s.id,type=ci.type,position=ci.position,name=ci.name or "",total=ci.total,text=ci.text,status="none")
            db.add(ni); db.flush()
            if ci.type=='media' and ci.file_path:
                db.add(Attachment(item_id=ni.id,name=ci.name or "file",mime=ci.mime or "application/octet-stream",size=ci.size or 0,file_path=ci.file_path))
            for sb in ci.subblocks:
                db.add(ItemSubblock(item_id=ni.id,type=sb.type,content=sb.content,name=sb.name,position=sb.position,file_path=sb.file_path,mime=sb.mime,size=sb.size))
    db.commit(); db.refresh(st); return get_student(stid,u,db)

@app.post("/api/students/{stid}/save-as-course",response_model=CourseOut,status_code=201)
def save_as_course(stid:str,d:SaveAsCourseRequest,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    chk_acc(stid,u,db)
    st=db.query(Student).options(
        joinedload(Student.sections).joinedload(Section.items).joinedload(Item.subblocks)
    ).filter(Student.id==stid).first()
    if not st: raise HTTPException(404)
    if not db.query(Subject).filter(Subject.id==d.subject_id).first(): raise HTTPException(404,"Предмет не найден")
    if d.replace_id:
        old=db.query(Course).filter(Course.id==d.replace_id).first()
        if old and (old.author_id==u.id or u.role=="owner"): db.delete(old); db.flush()
    c=Course(subject_id=d.subject_id,author_id=u.id,title=d.title,access=d.access)
    db.add(c); db.flush()
    for sec in sorted(st.sections,key=lambda s:s.position):
        csec=CourseSection(course_id=c.id,title=sec.title,position=sec.position,idz_enabled=sec.idz_enabled,control_enabled=sec.control_enabled,idz_text=sec.idz_text)
        db.add(csec); db.flush()
        pos=0
        for item in sorted(sec.items,key=lambda i:i.position):
            if item.type in ("topic","hw","media","note"):
                nci=CourseSectionItem(section_id=csec.id,type=item.type,position=pos,name=item.name or "",total=item.total,text=item.text)
                db.add(nci); db.flush()
                for sb in item.subblocks:
                    db.add(CourseItemSubblock(item_id=nci.id,type=sb.type,content=sb.content,name=sb.name,position=sb.position,file_path=sb.file_path,mime=sb.mime,size=sb.size))
                pos+=1
    db.commit(); db.refresh(c)
    return db.query(Course).options(joinedload(Course.sections).joinedload(CourseSection.items)).filter(Course.id==c.id).first()

# ═══ SECTIONS ═══
@app.post("/api/students/{stid}/sections",response_model=SectionOut,status_code=201)
def create_sec(stid:str,d:SectionCreate,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    chk_acc(stid,u,db)
    if not db.query(Student).filter(Student.id==stid).first(): raise HTTPException(404)
    mp=db.query(Section.position).filter(Section.student_id==stid).order_by(Section.position.desc()).first()
    sec=Section(student_id=stid,title=d.title,position=(mp[0]+1) if mp else 0,
                idz_enabled=d.idz_enabled,control_enabled=d.control_enabled,
                course_id=d.course_id,idz_text=d.idz_text)
    db.add(sec); db.commit(); db.refresh(sec); return sec

@app.patch("/api/sections/{sid}",response_model=SectionOut)
def upd_sec(sid:str,d:SectionUpdate,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    sec=db.query(Section).filter(Section.id==sid).first()
    if not sec: raise HTTPException(404)
    ud=d.model_dump(exclude_unset=True)
    if not is_tr(u):
        allowed={"is_open","idz"}
        if set(ud.keys())-allowed: raise HTTPException(403)
        if sec.locked and set(ud.keys())-{"is_open"}: raise HTTPException(403,"Раздел заблокирован")
    else:
        chk_acc(sec.student_id,u,db)
    # Auto-lock when control is set to 'passed'
    if ud.get("control")=="passed": ud["locked"]=True
    for f,v in ud.items(): setattr(sec,f,v)
    db.commit(); db.refresh(sec); return sec

@app.delete("/api/sections/{sid}",status_code=204)
def del_sec(sid:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    sec=db.query(Section).filter(Section.id==sid).first()
    if not sec: raise HTTPException(404)
    chk_acc(sec.student_id,u,db); _cln_sec(sec,db); db.delete(sec); db.commit()

# ═══ ITEMS ═══
@app.post("/api/sections/{sid}/items",response_model=ItemOut,status_code=201)
def create_item(sid:str,d:ItemCreate,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    sec=db.query(Section).filter(Section.id==sid).first()
    if not sec: raise HTTPException(404)
    chk_acc(sec.student_id,u,db)
    mp=db.query(Item.position).filter(Item.section_id==sid).order_by(Item.position.desc()).first()
    it=Item(section_id=sid,type=d.type,position=(mp[0]+1) if mp else 0,name=d.name,status=d.status or "none",
        total=d.total,done=d.done,closed=d.closed or False,date=d.date,closed_date=d.closed_date,note=d.note,text=d.text)
    db.add(it); db.commit(); db.refresh(it); return it

@app.patch("/api/items/{iid}",response_model=ItemOut)
def upd_item(iid:str,d:ItemUpdate,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    it=db.query(Item).filter(Item.id==iid).first()
    if not it: raise HTTPException(404)
    sec=db.query(Section).filter(Section.id==it.section_id).first()
    if sec:
        chk_acc(sec.student_id,u,db)
        if sec.locked and not is_tr(u): raise HTTPException(403,"Раздел заблокирован")
    ud=d.model_dump(exclude_unset=True)
    if not is_tr(u):
        student_allowed={"status","student_answer"}
        ud={k:v for k,v in ud.items() if k in student_allowed}
    for f,v in ud.items(): setattr(it,f,v)
    db.commit(); db.refresh(it); return it

@app.delete("/api/items/{iid}",status_code=204)
def del_item(iid:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    it=db.query(Item).filter(Item.id==iid).first()
    if not it: raise HTTPException(404)
    _cln_it(it,db); db.delete(it); db.commit()

@app.post("/api/sections/{sid}/items/reorder",status_code=200)
def reorder(sid:str,ids:list[str],u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    m={i.id:i for i in db.query(Item).filter(Item.section_id==sid).all()}
    for p,iid in enumerate(ids):
        if iid in m: m[iid].position=p
    db.commit(); return {"ok":True}

# ═══ ATTACHMENTS ═══
@app.post("/api/items/{iid}/attachments",response_model=AttachmentOut,status_code=201)
async def upload_att(iid:str,file:UploadFile=File(...),u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    it=db.query(Item).filter(Item.id==iid).first()
    if not it: raise HTTPException(404)
    sec=db.query(Section).filter(Section.id==it.section_id).first()
    if sec: chk_acc(sec.student_id,u,db)
    if u.role=="student" and it.type!="hw": raise HTTPException(403,"Ученики могут прикреплять файлы только к ДЗ")
    aid=gen_id(); ext=os.path.splitext(file.filename)[1] if file.filename else ""
    content=await file.read(); fp,dbp=_up(aid,ext)
    if len(content)>50*1024*1024: raise HTTPException(413)
    with open(fp,"wb") as f: f.write(content)
    att=Attachment(id=aid,item_id=iid,name=file.filename or "file",mime=file.content_type or "application/octet-stream",size=len(content),file_path=dbp)
    db.add(att); db.commit(); db.refresh(att); return att


@app.post("/api/items/{iid}/check-code")
async def check_code(iid:str,body:dict,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    it=db.query(Item).filter(Item.id==iid).first()
    if not it or it.type!='code': raise HTTPException(404)
    sec=db.query(Section).filter(Section.id==it.section_id).first()
    if sec: chk_acc(sec.student_id,u,db)
    answer=(it.note or '').strip(); submitted=(body.get('code') or '').strip()
    if not answer: return {"match":False,"error":"no_answer"}
    match=submitted==answer
    if match:
        it.student_answer=submitted; it.status='done'; db.commit()
    return {"match":match}

@app.delete("/api/attachments/{aid}",status_code=204)
def del_att(aid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    a=db.query(Attachment).filter(Attachment.id==aid).first()
    if not a: raise HTTPException(404)
    it=db.query(Item).filter(Item.id==a.item_id).first()
    if it:
        sec=db.query(Section).filter(Section.id==it.section_id).first()
        if sec: chk_acc(sec.student_id,u,db)
    _rm(a.file_path); db.delete(a); db.commit()

# ═══ TEMPLATES (legacy) ═══
TPL={"oge":[("Алгебра. База",1,1,["Числа","Степени и корни","Уравнения","Неравенства","Функции","Прогрессии"]),("Геометрия",1,1,["Треугольники","Четырёхугольники","Окружность","Подобие","Площади"]),("Статистика",1,1,["Таблицы","Вероятности"]),("Задачи 2-й части",0,0,["Задача 19","Задача 20","Задача 21"])],
"ege":[("Алгебра и анализ",1,1,["Тригонометрия","Показательные/лог.","Производная","Первообразная","Уравнения","Неравенства"]),("Геометрия",1,1,["Планиметрия","Стереометрия"]),("Статистика",1,1,["Комбинаторика","Вероятность","Статистика"]),("Профильные (ч.2)",1,0,["Уравнение","Неравенство","Геометрия","Параметр","Доказательство"])],
"olymp":[("Алгебра",1,1,["Тождества","Диофантовы ур.","Функц. ур.","Неравенства AM-GM"]),("Комбинаторика/числа",1,1,["Делимость","НОД/НОК","Дирихле","Инварианты"]),("Геометрия",1,1,["Вписанные углы","Радикальная ось","Аффинные преобр."]),("По уровням",1,0,["Муниципальный","Региональный","Всероссийский"])]}

@app.post("/api/students/{stid}/apply-template/{tkey}",response_model=StudentOut)
def apply_tpl(stid:str,tkey:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    chk_acc(stid,u,db); st=db.query(Student).filter(Student.id==stid).first()
    if not st: raise HTTPException(404)
    if tkey not in TPL: raise HTTPException(400)
    for sec in st.sections: _cln_sec(sec,db)
    db.query(Section).filter(Section.student_id==stid).delete()
    for pos,(title,idz,ctrl,items) in enumerate(TPL[tkey]):
        sec=Section(student_id=stid,title=title,position=pos,idz_enabled=bool(idz),control_enabled=bool(ctrl))
        db.add(sec); db.flush()
        for ip,nm in enumerate(items): db.add(Item(section_id=sec.id,type="topic",position=ip,name=nm,status="none"))
    db.commit(); db.refresh(st); return get_student(stid,u,db)

# ═══ HELPERS ═══
def _cln_it(it,db):
    for a in it.attachments: _rm(a.file_path)
    for sb in it.subblocks: _rm(sb.file_path)
def _cln_sec(sec,db):
    for it in db.query(Item).filter(Item.section_id==sec.id).all(): _cln_it(it,db)
def _cln_stu(st,db):
    for sec in db.query(Section).filter(Section.student_id==st.id).all(): _cln_sec(sec,db)
    db.query(Board).filter(Board.student_id==st.id).delete(synchronize_session=False)

# ═══ GLOBAL USER CONNECTIONS (for calls / notifications) ═══
user_global_conns: dict[str, set] = defaultdict(set)  # uid -> set of ws
# Pending call offers: callee_uid -> {offer, caller_ws, expires_at}
pending_call_offers: dict[str, dict] = {}
call_rooms: dict[str, dict[str, str]] = defaultdict(dict)  # room_id -> {uid: name}

async def _send_to_user(uid: str, msg: dict):
    dead = set()
    for ws in list(user_global_conns.get(uid, set())):
        try: await ws.send_text(json.dumps(msg))
        except: dead.add(ws)
    if dead: user_global_conns[uid] -= dead

@app.websocket("/ws/user/{uid}")
async def user_global_ws(ws: WebSocket, uid: str):
    token = ws.query_params.get("token","")
    await ws.accept()
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        sub = payload.get("sub")
    except:
        await ws.close(code=4001); return
    if sub != uid:
        await ws.close(code=4003); return
    user_global_conns[uid].add(ws)
    # Доставить отложенный входящий звонок если есть
    if uid in pending_call_offers:
        pco = pending_call_offers.pop(uid)
        if pco['expires_at'] > time.time():
            try: await ws.send_text(json.dumps(pco['offer']))
            except: pass
    try:
        while True:
            raw = await ws.receive_text()
            try: msg = json.loads(raw)
            except: continue
            mtype = msg.get("type","")
            # Call signaling: forward to target user
            if mtype == "typing":
                to_uid = msg.get("to")
                if to_uid: await _send_to_user(to_uid, {"type":"typing","from":uid})
            elif mtype == "read_receipt":
                to_uid = msg.get("to")
                if to_uid: await _send_to_user(to_uid, {"type":"read_receipt","from":uid})
            elif mtype in ("call_offer","call_answer","call_ice","call_reject","call_end","call_busy","call_reoffer","call_reanswer","call_cam"):
                to_uid = msg.get("to")
                if to_uid:
                    msg["from"] = uid
                    if user_global_conns.get(to_uid):
                        await _send_to_user(to_uid, msg)
                    elif mtype == "call_offer":
                        # Буферизуем offer на 25 секунд — абонент может быть в процессе reconnect
                        import time as _time
                        # Очистить устаревшие
                        expired = [k for k,v in pending_call_offers.items() if v['expires_at'] < _time.time()]
                        for k in expired: pending_call_offers.pop(k, None)
                        pending_call_offers[to_uid] = {
                            'offer': msg,
                            'caller_ws': ws,
                            'expires_at': _time.time() + 25,
                        }
                        # Через 25 секунд — если не доставлено, сообщить недоступен
                        async def _expire_offer(callee=to_uid, caller=ws, offer_msg=msg):
                            await asyncio.sleep(25)
                            if callee in pending_call_offers and pending_call_offers[callee].get('caller_ws') is caller:
                                pending_call_offers.pop(callee, None)
                                try: await caller.send_text(json.dumps({"type":"call_unavailable","to":callee}))
                                except: pass
                        asyncio.create_task(_expire_offer())
            elif mtype == "call_room_join":
                room_id = msg.get("room")
                joiner_name = msg.get("name", "?")
                if room_id:
                    existing = dict(call_rooms[room_id])
                    call_rooms[room_id][uid] = joiner_name
                    await ws.send_text(json.dumps({"type":"call_room_joined","room":room_id,
                        "peers":[{"uid":u,"name":n} for u,n in existing.items()]}))
                    for peer_uid in existing:
                        await _send_to_user(peer_uid, {"type":"call_room_peer_joined",
                            "room":room_id,"peer":uid,"peer_name":joiner_name})
            elif mtype in ("call_room_offer","call_room_answer","call_room_ice"):
                to_uid = msg.get("to")
                if to_uid:
                    msg["from"] = uid
                    await _send_to_user(to_uid, msg)
            elif mtype == "call_room_cam":
                room_id = msg.get("room")
                if room_id:
                    msg["from"] = uid
                    for peer_uid in list(call_rooms.get(room_id, {}).keys()):
                        if peer_uid != uid:
                            await _send_to_user(peer_uid, msg)
            elif mtype == "call_room_leave":
                room_id = msg.get("room")
                if room_id and uid in call_rooms.get(room_id, {}):
                    call_rooms[room_id].pop(uid, None)
                    remaining = list(call_rooms.get(room_id, {}).keys())
                    for peer_uid in remaining:
                        await _send_to_user(peer_uid, {"type":"call_room_peer_left","room":room_id,"peer":uid})
                    if not call_rooms.get(room_id):
                        call_rooms.pop(room_id, None)
    except WebSocketDisconnect: pass
    except Exception as e: print(f"[USER_WS] {e}")
    finally:
        user_global_conns[uid].discard(ws)
        if not user_global_conns[uid]:
            del user_global_conns[uid]
        # Clean up from call rooms on disconnect
        for _rid in list(call_rooms.keys()):
            if uid in call_rooms.get(_rid, {}):
                call_rooms[_rid].pop(uid, None)
                for _peer in list(call_rooms.get(_rid, {}).keys()):
                    asyncio.create_task(_send_to_user(_peer, {"type":"call_room_peer_left","room":_rid,"peer":uid}))
                if not call_rooms.get(_rid):
                    call_rooms.pop(_rid, None)


@app.post("/api/groups/{gid}/call-notify")
async def notify_group_call(gid: str, db: Session = Depends(get_db), me = Depends(get_current_user)):
    if _is_subscription_restricted(me): raise HTTPException(403,"Звонки недоступны — требуется активная подписка")
    group = db.query(ChatGroup).filter(ChatGroup.id == gid).first()
    if not group:
        raise HTTPException(status_code=404, detail="Группа не найдена")
    for member in group.members:
        if member.id != me.id:
            await _send_to_user(member.id, {
                "type": "call_room_ringing",
                "room": gid,
                "room_name": group.name,
                "from_name": me.name,
            })
    return {"ok": True}

# ═══ BOARD ═══
brd_conns: dict[str,set[WebSocket]] = defaultdict(set)
brd_users: dict[str,dict[str,str]] = defaultdict(dict)  # stid -> {uid: name}

def _get_board(stid,db,instance_id=None):
    # 1. Try exact match (instance-specific board)
    if instance_id:
        b=db.query(Board).filter(Board.student_id==stid,Board.instance_id==instance_id).first()
        if b: return b
    # 2. Fall back to general board (instance_id IS NULL) — preserves all existing content
    b=db.query(Board).filter(Board.student_id==stid,Board.instance_id==None).first()
    if b: return b
    # 3. Create general board if nothing exists
    b=Board(student_id=stid,instance_id=None,strokes="[]"); db.add(b); db.commit(); db.refresh(b)
    return b



def _get_group_board(gid: str, instance_id: str, db):
    """Получить или создать доску для пары (group_id, instance_id)."""
    b = db.query(Board).filter(Board.group_id == gid, Board.instance_id == instance_id).first()
    if b: return b
    b = Board(student_id=None, group_id=gid, instance_id=instance_id, strokes="[]")
    db.add(b); db.commit(); db.refresh(b)
    return b
@app.get("/api/boards/{stid}",response_model=BoardOut)
def get_board(stid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db),instance_id:str=None):
    if _is_subscription_restricted(u): raise HTTPException(403,"Доска недоступна — требуется активная подписка")
    chk_acc(stid,u,db)
    if not db.query(Student).filter(Student.id==stid).first(): raise HTTPException(404)
    return _get_board(stid,db,instance_id)

@app.post("/api/boards/{stid}/clear")
def clear_board(stid:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    b=db.query(Board).filter(Board.student_id==stid).first()
    if b: b.strokes="[]"; db.commit()
    return {"ok":True}

async def _bcast(stid,msg,sender):
    dead=set()
    for c in brd_conns.get(stid,set()):
        if c is not sender:
            try: await c.send_text(msg)
            except: dead.add(c)
    if dead and stid in brd_conns: brd_conns[stid]-=dead

@app.post("/api/run_code")
async def run_code(body:dict=Body(...),u:User=Depends(get_current_user)):
    code=body.get("code","")
    if not code.strip(): return {"stdout":"","stderr":"","ok":True}
    try:
        r=subprocess.run(["python3","-c",code],capture_output=True,text=True,timeout=10)
        return {"stdout":r.stdout,"stderr":r.stderr,"ok":r.returncode==0}
    except subprocess.TimeoutExpired:
        return {"stdout":"","stderr":"Превышено время выполнения (10 с)","ok":False}
    except Exception as e:
        return {"stdout":"","stderr":str(e),"ok":False}

class RunCodeIn(BaseModel):
    code: str
    lang: str = "python"

@app.post("/api/run_code")
async def run_code(body: RunCodeIn, u: User = Depends(get_current_user)):
    import subprocess, tempfile, os
    if u.role not in ("owner", "tutor", "student"):
        raise HTTPException(403, "Forbidden")
    code = body.code
    if len(code) > 20000:
        raise HTTPException(400, "Code too large")
    lang = body.lang
    if lang == "javascript":
        # Run with node if available, otherwise return error
        try:
            r = subprocess.run(
                ["node", "--input-type=module"],
                input=code.encode(), capture_output=True, timeout=10
            )
            return {"stdout": r.stdout.decode("utf-8","replace"),
                    "stderr": r.stderr.decode("utf-8","replace"),
                    "ok": r.returncode == 0}
        except FileNotFoundError:
            return {"stdout": "", "stderr": "Node.js не установлен на сервере", "ok": False}
        except subprocess.TimeoutExpired:
            return {"stdout": "", "stderr": "Превышено время выполнения (10с)", "ok": False}
    else:
        # Python execution
        try:
            with tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w", encoding="utf-8") as f:
                f.write(code)
                fname = f.name
            try:
                r = subprocess.run(
                    ["python3", fname],
                    capture_output=True, timeout=10,
                    cwd=tempfile.gettempdir()
                )
                return {"stdout": r.stdout.decode("utf-8","replace")[:10000],
                        "stderr": r.stderr.decode("utf-8","replace")[:5000],
                        "ok": r.returncode == 0}
            except subprocess.TimeoutExpired:
                return {"stdout": "", "stderr": "Превышено время выполнения (10с)", "ok": False}
            finally:
                try: os.unlink(fname)
                except: pass
        except Exception as e:
            return {"stdout": "", "stderr": str(e), "ok": False}

# ═══ BOARD-AWARE CODE EXECUTION ═══
class RunCodeBoardIn(BaseModel):
    code: str
    lang: str = "python"
    stid: str = ""
    personal_id: str = ""

@app.post("/api/run_code_board")
async def run_code_board(body:RunCodeBoardIn, u:User=Depends(get_current_user), db:Session=Depends(get_db)):
    if u.role not in ("owner","tutor","student"):
        raise HTTPException(403,"Forbidden")
    code=body.code.strip()
    if not code:
        return {"stdout":"","stderr":"","ok":True,"new_elements":[],"deleted_ids":[],"updated_elements":[]}
    if len(code)>20000: raise HTTPException(400,"Code too large")
    stid=body.stid; lang=body.lang; personal_id=body.personal_id

    # Проверяем доступ к доске (сессионной или личной)
    if stid:
        try: chk_acc(stid,u,db)
        except: stid=""
    if personal_id and not stid:
        pb=db.query(PersonalBoard).filter(PersonalBoard.id==personal_id).first()
        if not pb:
            personal_id=""
        else:
            is_owner=pb.owner_id==u.id
            is_shared=db.execute(personal_board_share.select().where(
                (personal_board_share.c.board_id==personal_id)&
                (personal_board_share.c.user_id==u.id))).first() is not None
            if not (is_owner or is_shared):
                personal_id=""

    def _get_strokes():
        if stid:
            return json.loads(_get_board(stid,db).strokes)
        if personal_id:
            pb=db.query(PersonalBoard).filter(PersonalBoard.id==personal_id).first()
            return json.loads(pb.strokes) if pb else []
        return []

    def _save_strokes(strokes_list):
        if stid:
            b=_get_board(stid,db,instance_id); b.strokes=json.dumps(strokes_list); db.commit()
        elif personal_id:
            pb=db.query(PersonalBoard).filter(PersonalBoard.id==personal_id).first()
            if pb: pb.strokes=json.dumps(strokes_list); db.commit()

    async def _bcast_el(el,op="stroke"):
        msg=json.dumps({"type":op,"data":el} if op=="stroke" else {"type":op,"id":el})
        if stid: asyncio.create_task(_bcast(stid,msg,None))
        elif personal_id: asyncio.create_task(_pb_bcast(personal_id,msg,None))

    # Собираем docfile-элементы с доски
    board_files={}  # filename -> {el, path}
    if stid or personal_id:
        try:
            for el in _get_strokes():
                if el.get('type')=='docfile':
                    fname=el.get('filename',''); url=el.get('url','')
                    if fname and url:
                        board_files[fname]={'el':el,'path':os.path.join(BASE_DIR,url.lstrip('/'))}
        except: pass

    # Рабочая директория = копия файлов доски
    workdir=tempfile.mkdtemp(prefix='hbm_code_')
    stdout=stderr=""; ok=False
    new_elements=[]; deleted_ids=[]; updated_elements=[]
    try:
        # Копируем файлы доски в рабочую директорию
        initial_snap={}  # fname -> size (для обнаружения изменений)
        for fname,info in board_files.items():
            if os.path.exists(info['path']):
                dst=os.path.join(workdir,fname)
                try:
                    shutil.copy2(info['path'],dst)
                    initial_snap[fname]=os.path.getsize(dst)
                except: pass

        # Запуск кода
        if lang=="javascript":
            try:
                r=subprocess.run(["node","--input-type=module"],input=code.encode(),
                    capture_output=True,timeout=10,cwd=workdir)
                stdout=r.stdout.decode("utf-8","replace")
                stderr=r.stderr.decode("utf-8","replace")
                ok=r.returncode==0
            except FileNotFoundError: stderr="Node.js не установлен на сервере"
            except subprocess.TimeoutExpired: stderr="Таймаут (10с)"
        else:
            # Python: записываем скрипт во временный файл
            with tempfile.NamedTemporaryFile(suffix=".py",delete=False,mode="w",encoding="utf-8") as f:
                f.write(code); fname_code=f.name
            try:
                r=subprocess.run(["python3",fname_code],
                    capture_output=True,timeout=30,cwd=workdir)
                stdout=r.stdout.decode("utf-8","replace")[:10000]
                stderr=r.stderr.decode("utf-8","replace")[:5000]
                ok=r.returncode==0
            except subprocess.TimeoutExpired: stderr="Таймаут (30с)"
            finally:
                try: os.unlink(fname_code)
                except: pass

        # Анализируем изменения в рабочей директории
        if stid or personal_id:
            cur_files={f for f in os.listdir(workdir) if not f.startswith('.')}
            strokes=_get_strokes()
            offset_base=sum(1 for e in strokes if e.get('type')=='docfile')
            added_count=0

            # Новые файлы → добавляем на доску
            for fname in sorted(cur_files):
                if fname in initial_snap: continue
                ext=os.path.splitext(fname)[1].lower()
                if ext not in _BOARD_CODE_OUT_EXTS: continue
                fpath=os.path.join(workdir,fname)
                try:
                    with open(fpath,'rb') as f: content=f.read()
                except: continue
                if len(content)>100*1024*1024: continue
                fid=gen_id(); fp,dbp=_up("bdoc_"+fid,ext)
                with open(fp,'wb') as f: f.write(content)
                mime=mimetypes.guess_type(fname)[0] or 'application/octet-stream'
                off=(offset_base+added_count)*12
                eid=gen_id()
                el={"type":"docfile","id":eid,"user_id":u.id,
                    "x":off,"y":off,"w":260,"h":72,
                    "url":"/"+dbp,"filename":fname,"filesize":len(content),"mime":mime}
                c=_get_strokes(); c.append(el); _save_strokes(c)
                asyncio.create_task(_bcast_el(el,"stroke"))
                new_elements.append(el); added_count+=1

            # Удалённые файлы → убираем с доски
            for fname,info in board_files.items():
                if fname not in cur_files:
                    eid=info['el'].get('id')
                    if eid:
                        c=[e for e in _get_strokes() if e.get('id')!=eid]
                        _save_strokes(c)
                        asyncio.create_task(_bcast_el(eid,"erase_stroke"))
                        deleted_ids.append(eid)

            # Изменённые файлы → обновляем хранилище
            for fname,info in board_files.items():
                if fname not in cur_files: continue
                fpath=os.path.join(workdir,fname)
                try:
                    with open(fpath,'rb') as f: new_c=f.read()
                    src=info['path']
                    old_c=open(src,'rb').read() if os.path.exists(src) else b''
                    if new_c!=old_c:
                        with open(src,'wb') as f: f.write(new_c)
                        el=dict(info['el']); el['filesize']=len(new_c)
                        c=_get_strokes()
                        for i,e in enumerate(c):
                            if e.get('id')==el.get('id'): c[i]=el; break
                        _save_strokes(c)
                        asyncio.create_task(_bcast_el(el['id'],"erase_stroke"))
                        asyncio.create_task(_bcast_el(el,"stroke"))
                        updated_elements.append(el)
                except: pass
    finally:
        shutil.rmtree(workdir,ignore_errors=True)

    return {"stdout":stdout,"stderr":stderr,"ok":ok,
            "new_elements":new_elements,"deleted_ids":deleted_ids,"updated_elements":updated_elements}

@app.websocket("/ws/board/{stid}")
async def board_ws(ws:WebSocket,stid:str):
    await ws.accept()
    token=ws.query_params.get("token")
    instance_id=ws.query_params.get("instance_id") or None
    if not token: await ws.close(code=4001); return
    try: payload=jwt.decode(token,SECRET_KEY,algorithms=[ALGORITHM])
    except: await ws.close(code=4001); return
    uid=payload.get("sub"); urole=payload.get("role")
    db=SessionLocal()
    try:
        user=db.query(User).filter(User.id==uid).first()
        if not user: await ws.close(code=4001); return
        if _is_subscription_restricted(user): await ws.close(code=4003); db.close(); return
        if user.role=="student" and user.student_id!=stid: await ws.close(code=4003); return
        if user.role=="parent":
            if not db.execute(parent_student_link.select().where((parent_student_link.c.parent_id==user.id)&(parent_student_link.c.student_id==stid))).first():
                await ws.close(code=4003); return
        if user.role=="tutor":
            st=db.query(Student).filter(Student.id==stid).first()
            if st and st.created_by!=user.id:
                if not db.execute(tutor_student_link.select().where((tutor_student_link.c.tutor_id==user.id)&(tutor_student_link.c.student_id==stid))).first():
                    await ws.close(code=4003); return
        _get_board(stid,db,instance_id)
        uname_board=user.name
    finally: db.close()
    room_key=f"{stid}::{instance_id}" if instance_id else stid
    brd_conns[room_key].add(ws)
    brd_users[room_key][uid]=uname_board
    # Антифрод: трекинг начала сессии (только для тьюторов на совместной доске)
    import datetime as _dt
    from datetime import timezone as _tz
    _is_tutor_session = urole in ("tutor","owner","teamlead")
    if _is_tutor_session:
        if stid not in _board_sessions: _board_sessions[stid] = {}
        _board_sessions[stid][uid] = datetime.now(_tz.utc)
        async def _anomaly_check_task():
            await asyncio.sleep(300)  # 5 минут
            _db_an = SessionLocal()
            try: _check_board_anomaly(stid, uid, _db_an)
            except Exception as _e: print(f"[anomaly] {_e}")
            finally: _db_an.close()
        asyncio.create_task(_anomaly_check_task())
    # notify others about join
    await _bcast(room_key,json.dumps({"type":"user_join","uid":uid,"name":uname_board,"online":list({"uid":k,"name":v} for k,v in brd_users[room_key].items())}),ws)
    await ws.send_text(json.dumps({"type":"hello","user_id":uid,"name":uname_board,"online":[{"uid":k,"name":v} for k,v in brd_users[room_key].items() if k!=uid]}))
    try:
        while True:
            raw=await ws.receive_text()
            try: msg=json.loads(raw)
            except: continue
            mt=msg.get("type")
            if mt=="load":
                db=SessionLocal()
                try: db.expire_all(); b=_get_board(stid,db,instance_id); sj=b.strokes
                finally: db.close()
                await ws.send_text(json.dumps({"type":"strokes","data":json.loads(sj)}))
            elif mt=="stroke":
                sd=msg.get("data",{})
                if not sd.get("user_id"): sd["user_id"]=uid
                db=SessionLocal()
                try: b=_get_board(stid,db,instance_id); c=json.loads(b.strokes); c.append(sd); b.strokes=json.dumps(c); db.commit()
                finally: db.close()
                await _bcast(room_key,json.dumps({"type":"stroke","data":sd}),ws)
            elif mt=="clear":
                if urole not in ("owner","tutor"): continue
                db=SessionLocal()
                try:
                    b=_get_board(stid,db,instance_id)
                    b.strokes="[]"; db.commit()
                finally: db.close()
                await _bcast(room_key,json.dumps({"type":"clear"}),ws)
            elif mt=="undo":
                db=SessionLocal(); rid=None
                try:
                    b=_get_board(stid,db,instance_id); c=json.loads(b.strokes)
                    for i in range(len(c)-1,-1,-1):
                        if c[i].get("user_id")==uid: rid=c[i].get("id"); c.pop(i); break
                    if rid: b.strokes=json.dumps(c); db.commit()
                finally: db.close()
                if rid:
                    await ws.send_text(json.dumps({"type":"erase_stroke","id":rid}))
                    await _bcast(room_key,json.dumps({"type":"erase_stroke","id":rid}),ws)
            elif mt=="erase_stroke":
                eid=msg.get("id")
                if not eid: continue
                db=SessionLocal()
                try: b=_get_board(stid,db,instance_id); c=json.loads(b.strokes); b.strokes=json.dumps([s for s in c if s.get("id")!=eid]); db.commit()
                finally: db.close()
                await _bcast(room_key,json.dumps({"type":"erase_stroke","id":eid}),ws)
            elif mt=="stroke_update":
                sd=msg.get("data",{})
                eid=sd.get("id")
                if not eid: continue
                if not sd.get("user_id"): sd["user_id"]=uid
                db=SessionLocal()
                try:
                    b=_get_board(stid,db,instance_id); c=json.loads(b.strokes)
                    idx=next((i for i,s in enumerate(c) if s.get("id")==eid),-1)
                    if idx>=0: c[idx]=sd
                    else: c.append(sd)
                    b.strokes=json.dumps(c); db.commit()
                finally: db.close()
                await _bcast(room_key,json.dumps({"type":"stroke_update","data":sd}),ws)
            elif mt in ("cursor","view"):
                msg["uid"]=uid; msg["name"]=uname_board
                await _bcast(room_key,json.dumps(msg),ws)
    except WebSocketDisconnect: pass
    except Exception as e: print(f"[WS] {e}")
    finally:
        brd_conns[room_key].discard(ws)
        brd_users[room_key].pop(uid,None)
        if not brd_conns.get(room_key): brd_users.pop(room_key,None)
        await _bcast(room_key,json.dumps({"type":"user_leave","uid":uid}),None)

# ═══ MESSAGING ═══
@app.post("/api/cs/solve")
async def cs_solve(body:dict=Body(...),u:User=Depends(get_current_user)):
    """Symbolic intersection of two CS graph equations via SymPy."""
    eq1=str(body.get("eq1","")).strip()[:200]
    eq2=str(body.get("eq2","")).strip()[:200]
    if not eq1 or not eq2:
        raise HTTPException(400,"Missing equations")
    import subprocess,sys,json as _json
    script=f"""
import sympy as sp, sys, json
from sympy.parsing.sympy_parser import parse_expr, standard_transformations, implicit_multiplication_application
x=sp.Symbol('x',real=True)
y=sp.Symbol('y',real=True)
trf=standard_transformations+(implicit_multiplication_application,)
ns={{'x':x,'y':y,'sqrt':sp.sqrt,'cbrt':lambda a:a**sp.Rational(1,3),
     'sin':sp.sin,'cos':sp.cos,'tan':sp.tan,
     'asin':sp.asin,'acos':sp.acos,'atan':sp.atan,
     'arcsin':sp.asin,'arccos':sp.acos,'arctan':sp.atan,
     'cot':lambda a:sp.cos(a)/sp.sin(a),'arccot':lambda a:sp.pi/2-sp.atan(a),
     'sinh':sp.sinh,'cosh':sp.cosh,'tanh':sp.tanh,
     'arcsinh':sp.asinh,'arccosh':sp.acosh,'arctanh':sp.atanh,
     'ln':sp.ln,'log':sp.ln,'log10':lambda a:sp.log(a,10),'log2':lambda a:sp.log(a,2),
     'exp':sp.exp,'abs':sp.Abs,'pi':sp.pi,'e':sp.E}}

def parse(s):
    s=s.replace('^','**')
    idx=s.find('=')
    if idx>=0:
        lhs,rhs=s[:idx].strip(),s[idx+1:].strip()
        if lhs=='y': return parse_expr(rhs,local_dict=ns,transformations=trf)
        if rhs=='y': return parse_expr(lhs,local_dict=ns,transformations=trf)
        return None
    return parse_expr(s,local_dict=ns,transformations=trf)

def verify_real(f1,f2,xf,tol=1e-5):
    try:
        xfp=sp.Float(xf,30)
        v1=complex(f1.subs(x,xfp).evalf(20))
        v2=complex(f2.subs(x,xfp).evalf(20))
        if abs(v1.imag)>tol or abs(v2.imag)>tol: return False
        if abs(v1.real-v2.real)>tol: return False
        return True
    except: return False

def try_even_power_sub(diff_expr,f1,f2):
    try:
        p=sp.Poly(diff_expr,x)
        dct=p.as_dict()
        degs=[m[0] for m in dct.keys()]
        if not all(d%2==0 for d in degs): return None
        u=sp.Symbol('u')
        u_expr=sum(c*u**(d[0]//2) for d,c in dct.items())
        u_sols=sp.solveset(sp.Eq(u_expr,0),u,domain=sp.S.Reals)
        if not isinstance(u_sols,sp.sets.FiniteSet): return None
        x_sols=[]
        for uv in u_sols:
            try:
                uv_s=sp.simplify(uv)  # NO nsimplify — it corrupts nested radicals
                uv_f=float(uv_s.evalf(30))
            except: continue
            if uv_f<-1e-10: continue
            if abs(uv_f)<1e-10:
                x_sols.append(sp.Integer(0))
            else:
                sq=sp.simplify(sp.sqrt(uv_s))
                x_sols.extend([sq,-sq])
        return x_sols if x_sols else None
    except: return None

try:
    f1=parse({repr(eq1)})
    f2=parse({repr(eq2)})
    if f1 is None or f2 is None:
        print(json.dumps({{'ok':False,'reason':'not yfx'}})); sys.exit(0)

    # Strategy 1: direct solveset
    x_solutions=None
    sols=sp.solveset(sp.Eq(f1,f2),x,domain=sp.S.Reals)
    if isinstance(sols,sp.sets.FiniteSet):
        cands=list(sols)
        # Only use Strategy 1 if NO CRootOf in results
        if not any(c.has(sp.CRootOf) or c.has(sp.RootOf) for c in cands):
            x_solutions=cands

    # Strategy 2: even-power polynomial substitution (handles x^8, x^6,... polynomials)
    if x_solutions is None:
        try:
            diff=sp.expand(f1-f2)
            x_solutions=try_even_power_sub(diff,f1,f2)
        except: pass

    # Strategy 3: use solveset result anyway (even with CRootOf — filter per-solution)
    if x_solutions is None and isinstance(sols,sp.sets.FiniteSet):
        x_solutions=list(sols)
    elif x_solutions is None:
        print(json.dumps({{'ok':False,'reason':'infinite_or_unsolvable'}})); sys.exit(0)

    out=[]; had_rootof=False
    for xv in x_solutions:
        if xv.has(sp.CRootOf) or xv.has(sp.RootOf):
            had_rootof=True; continue
        xv_s=sp.simplify(xv)  # simplify only — nsimplify corrupts nested radicals
        if xv_s.has(sp.CRootOf) or xv_s.has(sp.RootOf):
            had_rootof=True; continue
        try: xf=float(sp.re(xv_s.evalf(50)))
        except: continue
        # Filter false roots (domain restrictions for sqrt etc.)
        if not verify_real(f1,f2,xf): continue
        try:
            yv=sp.simplify(f1.subs(x,xv_s))
            if yv.has(sp.CRootOf):
                yf=float(sp.re(yv.evalf(30)))
                yv=sp.Float(yf,10)
            yv_s=sp.simplify(yv)
        except: continue
        try:
            yf=float(sp.re(yv_s.evalf(30)))
            if abs(float(sp.im(yv_s.evalf(30))))>1e-6: continue
        except: continue
        xl=sp.latex(xv_s); yl=sp.latex(yv_s)
        if len(xl)>150 or len(yl)>150: continue
        out.append({{'xLatex':xl,'yLatex':yl,'xFloat':xf,'yFloat':yf}})
    print(json.dumps({{'ok':True,'solutions':out,'partial':had_rootof}}))
except Exception as ex:
    print(json.dumps({{'ok':False,'reason':str(ex)}}))
"""
    try:
        proc=await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(
                None,lambda:subprocess.run(
                    [sys.executable,'-c',script],
                    capture_output=True,text=True,timeout=15
                )
            ),timeout=20
        )
        result=_json.loads(proc.stdout.strip() or '{}')
    except Exception as e:
        result={'ok':False,'reason':str(e)}
    return result

_ALLOWED_MSG_EXTS={'.jpg','.jpeg','.png','.gif','.webp','.mp4','.mov','.avi','.pdf','.zip','.doc','.docx','.xls','.xlsx','.txt','.py','.js'}
_BOARD_DOC_EXTS={'.pdf','.doc','.docx','.xls','.xlsx','.ppt','.pptx','.odt','.ods','.odp','.txt','.csv','.rtf','.zip','.7z'}
# Расширения файлов, которые код может создавать на доске (+ изображения и текстовые форматы)
_BOARD_CODE_OUT_EXTS=_BOARD_DOC_EXTS|{'.png','.jpg','.jpeg','.gif','.webp','.bmp','.svg','.json','.xml','.md','.html','.log','.py','.js'}

@app.post("/api/board/upload-doc")
async def upload_board_doc(
    file:UploadFile=File(...),
    stid:str=Form(None),
    element_id:str=Form(None),
    el_x:float=Form(None), el_y:float=Form(None),
    el_w:float=Form(None), el_h:float=Form(None),
    u:User=Depends(get_current_user),
    db:Session=Depends(get_db)
):
    ext=os.path.splitext(file.filename or "")[1].lower()
    if ext not in _BOARD_DOC_EXTS: raise HTTPException(400,"Недопустимый тип файла")
    content=await file.read()
    if len(content)>100*1024*1024: raise HTTPException(413,"Файл слишком большой (макс 100 МБ)")
    fid=gen_id(); fp,dbp=_up("bdoc_"+fid,ext)
    with open(fp,"wb") as f: f.write(content)
    result={"url":"/"+dbp,"filename":file.filename or "document","filesize":len(content),"mime":file.content_type or "application/octet-stream"}
    # Атомарно сохраняем stroke в доску, если переданы stid и element_id
    if stid and element_id:
        try:
            chk_acc(stid,u,db)
            el={"type":"docfile","id":element_id,"user_id":u.id,
                "x":el_x or 0,"y":el_y or 0,"w":el_w or 260,"h":el_h or 72,
                "url":result["url"],"filename":result["filename"],
                "filesize":result["filesize"],"mime":result["mime"]}
            b=_get_board(stid,db); c=json.loads(b.strokes); c.append(el); b.strokes=json.dumps(c); db.commit()
            result["saved"]=True
            # broadcast через WS если есть активные соединения
            brd_msg=json.dumps({"type":"stroke","data":el})
            asyncio.create_task(_bcast(stid,brd_msg,None))
        except Exception: result["saved"]=False
    return result

@app.post("/api/messages/upload")
async def upload_msg_file(file:UploadFile=File(...),u:User=Depends(get_current_user)):
    ext=os.path.splitext(file.filename or "")[1].lower()
    if ext not in _ALLOWED_MSG_EXTS: raise HTTPException(400,"Недопустимый тип файла")
    content=await file.read()
    if len(content)>50*1024*1024: raise HTTPException(413,"Файл слишком большой (макс 50 МБ)")
    fid=gen_id(); fp,dbp=_up("msg_"+fid,ext)
    with open(fp,"wb") as f: f.write(content)
    return {"url":"/"+dbp,"name":file.filename or "file","mime":file.content_type or "application/octet-stream","size":len(content)}

@app.post("/api/messages",status_code=201)
def send_message(d:MessageCreate,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    if d.to_id==u.id: raise HTTPException(400,"Нельзя писать себе")
    to=db.query(User).filter(User.id==d.to_id).first()
    if not to: raise HTTPException(404)
    if not d.text.strip(): raise HTTPException(400,"Пустое сообщение")
    msg=Message(from_id=u.id,to_id=d.to_id,text=d.text.strip()[:2000])
    db.add(msg); db.commit(); db.refresh(msg)
    preview=msg.text
    try:
        _j=json.loads(msg.text)
        if _j.get("type")=="board_invite": preview=f"📋 Приглашение на доску «{_j.get('board_title','')}»"
        elif _j.get("type")=="file": preview=f"📎 {_j.get('name','файл')}"
    except: pass
    send_push_to_user(db,d.to_id,u.name,preview[:200],{"type":"message","from_id":u.id})
    return MessageOut(id=msg.id,from_id=msg.from_id,to_id=msg.to_id,text=msg.text,
        is_read=msg.is_read,created_at=msg.created_at,
        from_name=u.name,to_name=to.name)

@app.post("/api/push/register",status_code=204)
def register_push_token(d:PushTokenIn,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    existing=db.query(PushToken).filter(PushToken.token==d.token).first()
    if existing:
        existing.user_id=u.id; existing.platform=d.platform
    else:
        db.add(PushToken(user_id=u.id,token=d.token,platform=d.platform))
    db.commit()

@app.post("/api/push/unregister",status_code=204)
def unregister_push_token(d:PushTokenIn,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    db.query(PushToken).filter(PushToken.token==d.token).delete()
    db.commit()

@app.get("/api/messages")
def list_conversations(u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    # Get all messages involving current user
    msgs=db.query(Message).options(joinedload(Message.from_user),joinedload(Message.to_user))\
        .filter((Message.from_id==u.id)|(Message.to_id==u.id))\
        .order_by(Message.created_at.desc()).all()
    # Group by conversation partner
    seen={}
    for m in msgs:
        partner_id=m.to_id if m.from_id==u.id else m.from_id
        partner=m.to_user if m.from_id==u.id else m.from_user
        if partner_id not in seen:
            unread=0 if m.from_id==u.id else (0 if m.is_read else 1)
            lt=m.text[:80]
            try:
                _j=json.loads(m.text)
                if _j.get("type")=="board_invite": lt=f"📋 Приглашение на доску «{_j.get('board_title','')}»"[:80]
                elif _j.get("type")=="file": lt=f"📎 {_j.get('name','файл')}"[:80]
            except: pass
            seen[partner_id]={"partner_id":partner_id,"partner_name":partner.name if partner else partner_id,
                "last_text":lt,"last_at":m.created_at.isoformat() if m.created_at else None,
                "unread":unread}
        elif not m.is_read and m.to_id==u.id:
            seen[partner_id]["unread"]+=1
    return list(seen.values())

@app.get("/api/messages/{uid}")
def get_thread(uid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    msgs=db.query(Message).options(joinedload(Message.from_user),joinedload(Message.to_user))\
        .filter(((Message.from_id==u.id)&(Message.to_id==uid))|((Message.from_id==uid)&(Message.to_id==u.id)))\
        .order_by(Message.created_at.asc()).all()
    # Mark incoming as read
    for m in msgs:
        if m.to_id==u.id and not m.is_read: m.is_read=True
    db.commit()
    return [MessageOut(id=m.id,from_id=m.from_id,to_id=m.to_id,text=m.text,
        is_read=m.is_read,created_at=m.created_at.isoformat() if m.created_at else None,
        from_name=m.from_user.name if m.from_user else "",
        to_name=m.to_user.name if m.to_user else "") for m in msgs]

@app.get("/api/notifications")
def get_notifications(u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    unread_msgs=db.query(Message).filter(Message.to_id==u.id,Message.is_read==False).count()
    notifs=db.query(Notification).filter(Notification.user_id==u.id).order_by(Notification.created_at.desc()).limit(50).all()
    unread_notifs=sum(1 for n in notifs if not n.is_read)
    grp_unread=0
    try:
        user_groups=db.execute(_sa_text("SELECT group_id FROM chat_group_members WHERE user_id=:u").bindparams(u=u.id)).fetchall()
        for gm in user_groups:
            gid=gm[0]
            reads=db.execute(_sa_text("SELECT last_read_at FROM group_message_reads WHERE group_id=:g AND user_id=:u").bindparams(g=gid,u=u.id)).first()
            if reads:
                cnt=db.execute(_sa_text("SELECT COUNT(*) FROM group_messages WHERE group_id=:g AND from_id!=:u AND created_at>:t").bindparams(g=gid,u=u.id,t=reads[0])).scalar()
            else:
                cnt=db.execute(_sa_text("SELECT COUNT(*) FROM group_messages WHERE group_id=:g AND from_id!=:u").bindparams(g=gid,u=u.id)).scalar()
            grp_unread+=cnt or 0
    except: pass
    return {
        "unread_messages":unread_msgs+grp_unread,
        "unread_notifications":unread_notifs,
        "unread":unread_msgs+grp_unread+unread_notifs,
        "notifications":[{"id":n.id,"text":n.text,"is_read":n.is_read,
            "created_at":n.created_at.isoformat() if n.created_at else None,
            "link":n.link,"notif_type":n.notif_type} for n in notifs]}

# ═══ GROUP CHAT HELPERS ═══
def _sync_auto_group(student_id: str, db: Session):
    """Create/update per-tutor auto-groups: student + tutor + parents.
    Owner is NOT auto-included. Groups require >= 3 members.
    One group per (student, tutor) pair."""
    st = db.query(Student).filter(Student.id == student_id).first()
    if not st: return
    stu_user = db.query(User).filter(User.student_id == student_id).first()
    # Collect all tutors for this student
    tutor_ids = set()
    if st.created_by: tutor_ids.add(st.created_by)
    for r in db.execute(tutor_student_link.select().where(tutor_student_link.c.student_id == student_id)).fetchall():
        tutor_ids.add(r.tutor_id)
    # Collect all parents for this student
    parent_ids = set()
    for r in db.execute(parent_student_link.select().where(parent_student_link.c.student_id == student_id)).fetchall():
        parent_ids.add(r.parent_id)
    # Create/update one group per tutor
    for tutor_id in tutor_ids:
        tutor = db.query(User).filter(User.id == tutor_id).first()
        if not tutor: continue
        uids = set()
        if stu_user: uids.add(stu_user.id)
        uids.add(tutor_id)
        uids.update(parent_ids)
        # Require at least 3 members (student + tutor + at least 1 parent)
        if len(uids) < 3: continue
        grp = db.query(ChatGroup).filter(
            ChatGroup.student_id == student_id,
            ChatGroup.tutor_id == tutor_id
        ).first()
        if not grp:
            grp = ChatGroup(name=f"{st.name} — {tutor.name}", student_id=student_id, tutor_id=tutor_id)
            db.add(grp); db.flush()
        try:
            existing = {r[0] for r in db.execute(_sa_text("SELECT user_id FROM chat_group_members WHERE group_id=:g").bindparams(g=grp.id)).fetchall()}
            for uid in uids - existing:
                db.execute(_sa_text("INSERT INTO chat_group_members(group_id,user_id) VALUES(:g,:u) ON CONFLICT DO NOTHING").bindparams(g=grp.id, u=uid))
            db.commit()
        except: db.rollback()

@app.post("/api/notifications/{nid}/read")
def mark_notif_read(nid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    n=db.query(Notification).filter(Notification.id==nid,Notification.user_id==u.id).first()
    if not n: raise HTTPException(404)
    n.is_read=True; db.commit(); return {"ok":True}

@app.post("/api/notifications/read-all")
def mark_all_read(u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    db.query(Notification).filter(Notification.user_id==u.id,Notification.is_read==False).update({"is_read":True})
    db.commit(); return {"ok":True}

# ═══ PROFILE ═══
@app.get("/api/profile")
def get_own_profile(u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    sids=[r.subject_id for r in db.execute(tutor_subject_link.select().where(tutor_subject_link.c.tutor_id==u.id)).fetchall()]
    courses=[]
    if u.role in ("tutor","owner"):
        courses=[{"id":c.id,"title":c.title,"access":c.access,"subject_name":c.subject.name if c.subject else ""}
                 for c in db.query(Course).options(joinedload(Course.subject)).filter(Course.author_id==u.id).all()]
    return {"id":u.id,"login":u.login,"role":u.role,"name":u.name,"about":u.about,"photo":u.photo,
            "must_change_password":u.must_change_password,"student_id":u.student_id,
            "created_at":u.created_at.isoformat() if u.created_at else None,
            "last_seen":u.last_seen.isoformat() if u.last_seen else None,
            "subject_ids":sids,"courses":courses}

@app.patch("/api/profile")
def update_own_profile(d:ProfileUpdate,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    if d.name is not None and d.name.strip(): u.name=d.name.strip()[:200]
    if d.about is not None: u.about=d.about[:2000] if d.about else None
    if d.photo is not None: u.photo=d.photo if d.photo else None
    db.commit(); return {"ok":True}

@app.get("/api/users/{uid}/profile")
def get_user_profile(uid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    t=db.query(User).filter(User.id==uid).first()
    if not t: raise HTTPException(404)
    if uid!=u.id:
        if u.role=="owner": pass
        elif u.role=="tutor":
            stids=set(_tutor_student_ids(u.id,db))
            s_uids={r.id for r in db.query(User.id).filter(User.student_id.in_(stids)).all()} if stids else set()
            p_ids={r.parent_id for r in db.execute(parent_student_link.select().where(parent_student_link.c.student_id.in_(stids))).fetchall()} if stids else set()
            o_ids={r.id for r in db.query(User.id).filter(User.role=="owner").all()}
            if uid not in s_uids|p_ids|o_ids: raise HTTPException(403)
        else:
            contacts=get_contacts(u,db)
            if uid not in {c["id"] for c in contacts}: raise HTTPException(403)
    sids=[r.subject_id for r in db.execute(tutor_subject_link.select().where(tutor_subject_link.c.tutor_id==t.id)).fetchall()]
    courses=[]
    if t.role in ("tutor","owner"):
        if u.role in ("tutor","owner"):
            access_filter=Course.access.in_(["public","internal"])
        else:
            access_filter=Course.access=="public"
        courses=[{"id":c.id,"title":c.title,"access":c.access,"subject_name":c.subject.name if c.subject else ""}
                 for c in db.query(Course).options(joinedload(Course.subject)).filter(Course.author_id==t.id,access_filter).all()]
    result={"id":t.id,"login":t.login,"role":t.role,"name":t.name,"about":t.about,"photo":t.photo,
            "must_change_password":t.must_change_password,
            "created_at":t.created_at.isoformat() if t.created_at else None,
            "last_seen":t.last_seen.isoformat() if t.last_seen else None,
            "subject_ids":sids,"courses":courses,
            "subscription_model":t.subscription_model,
            "commission_rate":t.commission_rate,
            "no_commission":t.no_commission,
            "payment_model":t.payment_model}
    if u.role=="owner":
        result["owner_notes"]=t.owner_notes
        reqs=db.query(ChangeRequest).filter(ChangeRequest.user_id==t.id,ChangeRequest.status=="pending").order_by(ChangeRequest.created_at.desc()).all()
        result["pending_requests"]=[{"id":r.id,"req_type":r.req_type,
            "new_value":r.new_value if r.req_type=="login" else "••••••",
            "created_at":r.created_at.isoformat() if r.created_at else None} for r in reqs]
    return result

@app.patch("/api/users/{uid}/profile")
def update_user_profile(uid:str,d:OwnerProfileUpdate,u:User=Depends(require_owner),db:Session=Depends(get_db)):
    t=db.query(User).filter(User.id==uid).first()
    if not t: raise HTTPException(404)
    if d.name is not None and d.name.strip(): t.name=d.name.strip()[:200]
    if d.about is not None: t.about=d.about[:2000] if d.about else None
    if d.owner_notes is not None: t.owner_notes=d.owner_notes[:5000] if d.owner_notes else None
    if d.photo is not None: t.photo=d.photo if d.photo else None
    if d.login is not None and d.login.strip():
        if db.query(User).filter(User.login==d.login,User.id!=uid).first(): raise HTTPException(409,"Логин занят")
        t.login=d.login.strip()[:100]
        n=Notification(user_id=t.id,text="Ваш логин был изменён администратором",notif_type="system")
        db.add(n)
    if d.password is not None and d.password:
        _check_password(d.password)
        t.password_hash=hash_password(d.password); t.must_change_password=False
        n=Notification(user_id=t.id,text="Ваш пароль был изменён администратором",notif_type="system")
        db.add(n)
    if d.no_commission is not None: t.no_commission=d.no_commission
    if d.subscription_model is not None:
        t.subscription_model=d.subscription_model if d.subscription_model else None
        if t.subscription_model and t.subscription_status is None: t.subscription_status="active"
    if d.commission_rate is not None: t.commission_rate=max(0,min(100,d.commission_rate))
    if d.is_tutor is not None: t.is_tutor=d.is_tutor
    if d.payment_model is not None: t.payment_model=d.payment_model if d.payment_model else None
    db.commit(); return {"ok":True}

# ═══ CHANGE REQUESTS ═══
@app.post("/api/profile/change-request",status_code=201)
def create_change_request(d:ChangeRequestCreate,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    if d.req_type not in ("login","password"): raise HTTPException(400,"Тип: login или password")
    if d.req_type=="login":
        if not d.new_value.strip(): raise HTTPException(400,"Введите логин")
        if db.query(User).filter(User.login==d.new_value,User.id!=u.id).first(): raise HTTPException(409,"Логин уже занят")
    if d.req_type=="password": _check_password(d.new_value)
    existing=db.query(ChangeRequest).filter(ChangeRequest.user_id==u.id,ChangeRequest.req_type==d.req_type,ChangeRequest.status=="pending").first()
    if existing:
        existing.new_value=d.new_value; db.commit(); return {"ok":True}
    cr=ChangeRequest(user_id=u.id,req_type=d.req_type,new_value=d.new_value)
    db.add(cr); db.flush()
    label="логина" if d.req_type=="login" else "пароля"
    _notify_teamlead_or_owner(u,f"Пользователь {u.name} запросил смену {label}","change_request",f"/profile.html?uid={u.id}",db)
    db.commit(); return {"ok":True}

@app.get("/api/change-requests")
def list_change_requests(u:User=Depends(require_teamlead_or_owner),db:Session=Depends(get_db)):
    if u.role=="owner":
        reqs=db.query(ChangeRequest).filter(ChangeRequest.status=="pending").order_by(ChangeRequest.created_at.desc()).all()
    else:
        # Teamlead видит запросы только своей команды
        tids=_team_tutor_ids(u.id,db)
        reqs=db.query(ChangeRequest).filter(ChangeRequest.status=="pending",ChangeRequest.user_id.in_(tids+[u.id])).order_by(ChangeRequest.created_at.desc()).all()
    return [{"id":r.id,"user_id":r.user_id,"user_name":r.req_user.name,"req_type":r.req_type,
             "new_value":r.new_value if r.req_type=="login" else "••••••",
             "created_at":r.created_at.isoformat() if r.created_at else None} for r in reqs]

@app.post("/api/change-requests/{rid}/approve")
def approve_change_request(rid:str,u:User=Depends(require_teamlead_or_owner),db:Session=Depends(get_db)):
    r=db.query(ChangeRequest).filter(ChangeRequest.id==rid).first()
    if not r: raise HTTPException(404)
    t=db.query(User).filter(User.id==r.user_id).first()
    if not t: raise HTTPException(404)
    # Teamlead одобряет только запросы своей команды
    if u.role=="teamlead" and t.teamlead_id!=u.id and t.id!=u.id: raise HTTPException(403,"Нет доступа")
    if r.req_type=="login":
        if db.query(User).filter(User.login==r.new_value,User.id!=t.id).first(): raise HTTPException(409,"Логин занят")
        t.login=r.new_value
    elif r.req_type=="password":
        t.password_hash=hash_password(r.new_value); t.must_change_password=False
    r.status="approved"
    label="логина" if r.req_type=="login" else "пароля"
    n=Notification(user_id=t.id,text=f"Ваш запрос на смену {label} одобрен",notif_type="request_approved")
    db.add(n); db.commit(); return {"ok":True}

@app.post("/api/change-requests/{rid}/reject")
def reject_change_request(rid:str,u:User=Depends(require_teamlead_or_owner),db:Session=Depends(get_db)):
    r=db.query(ChangeRequest).filter(ChangeRequest.id==rid).first()
    if not r: raise HTTPException(404)
    t=db.query(User).filter(User.id==r.user_id).first()
    if u.role=="teamlead" and t and t.teamlead_id!=u.id and t.id!=u.id: raise HTTPException(403,"Нет доступа")
    r.status="rejected"
    if t:
        label="логина" if r.req_type=="login" else "пароля"
        n=Notification(user_id=t.id,text=f"Ваш запрос на смену {label} отклонён",notif_type="request_rejected")
        db.add(n)
    db.commit(); return {"ok":True}

# ═══ CONTACTS ═══
@app.get("/api/contacts")
def get_contacts(u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    """Returns list of users the current user can message, enriched with last message info."""
    ids=set()
    if u.role=="owner":
        all_u=db.query(User).filter(User.id!=u.id).all()
        users=all_u
    else:
        if u.role=="teamlead":
            tids=_team_tutor_ids(u.id,db)
            sids=_teamlead_student_ids(u.id,db)
            for tid in tids: ids.add(tid)
            if sids:
                for r in db.query(User).filter(User.student_id.in_(sids)).all(): ids.add(r.id)
                for r in db.execute(parent_student_link.select().where(parent_student_link.c.student_id.in_(sids))).fetchall(): ids.add(r.parent_id)
            owner=db.query(User).filter(User.role=="owner").first()
            if owner: ids.add(owner.id)
        if u.role=="tutor":
            stids=set(_tutor_student_ids(u.id,db))
            if stids:
                for r in db.query(User).filter(User.student_id.in_(stids),User.id!=u.id).all():
                    ids.add(r.id)
                for r in db.execute(parent_student_link.select().where(parent_student_link.c.student_id.in_(stids))).fetchall():
                    ids.add(r.parent_id)
            owner=db.query(User).filter(User.role=="owner").first()
            if owner: ids.add(owner.id)
        elif u.role=="student":
            if u.student_id:
                st=db.query(Student).filter(Student.id==u.student_id).first()
                if st:
                    if st.created_by: ids.add(st.created_by)
                    for r in db.execute(tutor_student_link.select().where(tutor_student_link.c.student_id==u.student_id)).fetchall():
                        ids.add(r.tutor_id)
            owner=db.query(User).filter(User.role=="owner").first()
            if owner: ids.add(owner.id)
        elif u.role=="parent":
            child_ids=[r.student_id for r in db.execute(parent_student_link.select().where(parent_student_link.c.parent_id==u.id)).fetchall()]
            if child_ids:
                for stid in child_ids:
                    st=db.query(Student).filter(Student.id==stid).first()
                    if st and st.created_by: ids.add(st.created_by)
                    for r in db.execute(tutor_student_link.select().where(tutor_student_link.c.student_id==stid)).fetchall():
                        ids.add(r.tutor_id)
            owner=db.query(User).filter(User.role=="owner").first()
            if owner: ids.add(owner.id)
        elif u.role=="board_user":
            shared_rows=db.execute(personal_board_share.select().where(personal_board_share.c.user_id==u.id)).fetchall()
            board_ids={r.board_id for r in shared_rows}
            owned=db.query(PersonalBoard).filter(PersonalBoard.owner_id==u.id).all()
            board_ids|={b.id for b in owned}
            for bid in board_ids:
                b=db.query(PersonalBoard).filter(PersonalBoard.id==bid).first()
                if b and b.owner_id!=u.id: ids.add(b.owner_id)
                for m in db.execute(personal_board_share.select().where(personal_board_share.c.board_id==bid)).fetchall():
                    if m.user_id!=u.id: ids.add(m.user_id)
            owner=db.query(User).filter(User.role=="owner").first()
            if owner: ids.add(owner.id)
        ids.discard(u.id)
        users=db.query(User).filter(User.id.in_(ids)).all() if ids else []
    # Enrich with last message data
    msgs=db.query(Message).filter((Message.from_id==u.id)|(Message.to_id==u.id))\
        .order_by(Message.created_at.desc()).all()
    msg_map={}
    for m in msgs:
        pid=m.to_id if m.from_id==u.id else m.from_id
        if pid not in msg_map:
            unread=0 if m.from_id==u.id else (0 if m.is_read else 1)
            msg_map[pid]={"last_at":m.created_at.isoformat() if m.created_at else None,"unread":unread}
        elif not m.is_read and m.to_id==u.id:
            msg_map[pid]["unread"]+=1
    def _fmt(x):
        md=msg_map.get(x.id,{})
        return {"id":x.id,"name":x.name,"role":x.role,"photo":x.photo,
                "last_seen":x.last_seen.isoformat() if x.last_seen else None,
                "last_at":md.get("last_at"),"unread":md.get("unread",0)}
    result=[_fmt(x) for x in users]
    result.sort(key=lambda c:(c["last_at"] is not None, c["last_at"] or ""),reverse=True)
    return result

# ═══ GROUP CHATS ═══
class GroupCreate(BaseModel):
    name: str
    member_ids: list[str]=[]

class GroupMsgCreate(BaseModel):
    text: str

class GroupRename(BaseModel):
    name: Optional[str]=None
    photo: Optional[str]=None

class GroupAddMember(BaseModel):
    user_id: str

@app.post("/api/groups",status_code=201)
def create_group(d:GroupCreate,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    grp=ChatGroup(name=d.name.strip()[:300],created_by=u.id)
    db.add(grp); db.flush()
    member_ids=list(set(d.member_ids+[u.id]))
    for uid in member_ids:
        if db.query(User).filter(User.id==uid).first():
            db.execute(_sa_text("INSERT INTO chat_group_members(group_id,user_id) VALUES(:g,:u) ON CONFLICT DO NOTHING").bindparams(g=grp.id,u=uid))
    db.commit()
    return {"id":grp.id,"name":grp.name,"created_by":grp.created_by,"student_id":grp.student_id}

@app.get("/api/groups")
def list_groups(u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    # Owner sees ALL groups; others see only groups they're a member of
    if u.role=="owner":
        rows=db.execute(_sa_text("SELECT id AS group_id FROM chat_groups")).fetchall()
    else:
        rows=db.execute(_sa_text("SELECT group_id FROM chat_group_members WHERE user_id=:u").bindparams(u=u.id)).fetchall()
    result=[]
    for r in rows:
        gid=r[0]
        grp=db.query(ChatGroup).filter(ChatGroup.id==gid).first()
        if not grp: continue
        mc=db.execute(_sa_text("SELECT COUNT(*) FROM chat_group_members WHERE group_id=:g").bindparams(g=gid)).scalar() or 0
        lm=db.execute(_sa_text("SELECT text,created_at FROM group_messages WHERE group_id=:g ORDER BY created_at DESC LIMIT 1").bindparams(g=gid)).first()
        last_text=None; last_at=None
        if lm:
            last_text=lm[0][:80]
            try:
                _j=json.loads(lm[0])
                if _j.get("type")=="file": last_text=f"📎 {_j.get('name','файл')}"[:80]
            except: pass
            last_at=lm[1].isoformat() if lm[1] else None
        reads=db.execute(_sa_text("SELECT last_read_at FROM group_message_reads WHERE group_id=:g AND user_id=:u").bindparams(g=gid,u=u.id)).first()
        if reads:
            unread=db.execute(_sa_text("SELECT COUNT(*) FROM group_messages WHERE group_id=:g AND from_id!=:u AND created_at>:t").bindparams(g=gid,u=u.id,t=reads[0])).scalar() or 0
        else:
            unread=db.execute(_sa_text("SELECT COUNT(*) FROM group_messages WHERE group_id=:g AND from_id!=:u").bindparams(g=gid,u=u.id)).scalar() or 0
        result.append({"id":grp.id,"name":grp.name,"photo":grp.photo,"created_by":grp.created_by,"student_id":grp.student_id,
            "member_count":mc,"last_msg_text":last_text,"last_msg_at":last_at,"unread":unread})
    result.sort(key=lambda x:(x["last_msg_at"] is not None,x["last_msg_at"] or ""),reverse=True)
    return result

@app.get("/api/groups/{gid}")
def get_group_info(gid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    mem=db.execute(_sa_text("SELECT 1 FROM chat_group_members WHERE group_id=:g AND user_id=:u").bindparams(g=gid,u=u.id)).first()
    if not mem: raise HTTPException(403,"Не участник группы")
    grp=db.query(ChatGroup).filter(ChatGroup.id==gid).first()
    if not grp: raise HTTPException(404)
    mrows=db.execute(_sa_text("SELECT u.id,u.name,u.role,u.photo FROM chat_group_members cm JOIN users u ON u.id=cm.user_id WHERE cm.group_id=:g").bindparams(g=gid)).fetchall()
    members=[{"id":r[0],"name":r[1],"role":r[2],"photo":r[3]} for r in mrows]
    return {"id":grp.id,"name":grp.name,"photo":grp.photo,"created_by":grp.created_by,"student_id":grp.student_id,"members":members}

@app.get("/api/groups/{gid}/messages")
def get_group_messages(gid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    mem=db.execute(_sa_text("SELECT 1 FROM chat_group_members WHERE group_id=:g AND user_id=:u").bindparams(g=gid,u=u.id)).first()
    if not mem: raise HTTPException(403,"Не участник группы")
    msgs=db.execute(_sa_text("SELECT gm.id,gm.from_id,gm.text,gm.created_at,u.name,u.photo FROM group_messages gm LEFT JOIN users u ON u.id=gm.from_id WHERE gm.group_id=:g ORDER BY gm.created_at ASC").bindparams(g=gid)).fetchall()
    db.execute(_sa_text("INSERT INTO group_message_reads(group_id,user_id,last_read_at) VALUES(:g,:u,now()) ON CONFLICT(group_id,user_id) DO UPDATE SET last_read_at=now()").bindparams(g=gid,u=u.id))
    db.commit()
    return [{"id":m[0],"from_id":m[1],"text":m[2],"created_at":m[3].isoformat() if m[3] else None,"from_name":m[4] or "","from_photo":m[5]} for m in msgs]

@app.post("/api/groups/{gid}/messages",status_code=201)
def send_group_message(gid:str,d:GroupMsgCreate,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    mem=db.execute(_sa_text("SELECT 1 FROM chat_group_members WHERE group_id=:g AND user_id=:u").bindparams(g=gid,u=u.id)).first()
    if not mem: raise HTTPException(403,"Не участник группы")
    if not d.text.strip(): raise HTTPException(400,"Пустое сообщение")
    mid=gen_id()
    db.execute(_sa_text("INSERT INTO group_messages(id,group_id,from_id,text) VALUES(:id,:g,:f,:t)").bindparams(id=mid,g=gid,f=u.id,t=d.text.strip()[:4000]))
    db.execute(_sa_text("INSERT INTO group_message_reads(group_id,user_id,last_read_at) VALUES(:g,:u,now()) ON CONFLICT(group_id,user_id) DO UPDATE SET last_read_at=now()").bindparams(g=gid,u=u.id))
    db.commit()
    grp=db.query(ChatGroup).filter(ChatGroup.id==gid).first()
    preview=d.text.strip()
    try:
        _j=json.loads(preview)
        if _j.get("type")=="file": preview=f"📎 {_j.get('name','файл')}"
    except: pass
    members=db.execute(_sa_text("SELECT user_id FROM chat_group_members WHERE group_id=:g AND user_id!=:u").bindparams(g=gid,u=u.id)).fetchall()
    for (mid_,) in members:
        send_push_to_user(db,mid_,f"{u.name} · {grp.name if grp else 'группа'}",preview[:200],{"type":"group_message","group_id":gid})
    return {"id":mid,"group_id":gid,"from_id":u.id,"text":d.text.strip(),"from_name":u.name}

@app.patch("/api/groups/{gid}")
def rename_group(gid:str,d:GroupRename,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    grp=db.query(ChatGroup).filter(ChatGroup.id==gid).first()
    if not grp: raise HTTPException(404)
    if grp.created_by!=u.id and u.role!="owner": raise HTTPException(403)
    if d.name is not None: grp.name=d.name.strip()[:300]
    if d.photo is not None: grp.photo=d.photo or None
    db.commit()
    return {"ok":True,"name":grp.name,"photo":grp.photo}

@app.delete("/api/groups/{gid}",status_code=200)
def delete_group(gid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    grp=db.query(ChatGroup).filter(ChatGroup.id==gid).first()
    if not grp: raise HTTPException(404)
    is_owner=u.role=="owner"
    is_creator=grp.created_by==u.id
    is_tutor_of_group=grp.tutor_id==u.id
    if not (is_owner or is_creator or is_tutor_of_group):
        raise HTTPException(403,"Нет прав для удаления группы")
    db.delete(grp); db.commit()
    return {"ok":True}

@app.post("/api/groups/{gid}/members",status_code=200)
def add_group_member(gid:str,d:GroupAddMember,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    grp=db.query(ChatGroup).filter(ChatGroup.id==gid).first()
    if not grp: raise HTTPException(404)
    mem=db.execute(_sa_text("SELECT 1 FROM chat_group_members WHERE group_id=:g AND user_id=:u").bindparams(g=gid,u=u.id)).first()
    if not mem: raise HTTPException(403,"Не участник группы")
    if grp.created_by!=u.id and u.role!="owner": raise HTTPException(403)
    if not db.query(User).filter(User.id==d.user_id).first(): raise HTTPException(404,"Пользователь не найден")
    db.execute(_sa_text("INSERT INTO chat_group_members(group_id,user_id) VALUES(:g,:u) ON CONFLICT DO NOTHING").bindparams(g=gid,u=d.user_id))
    db.commit()
    return {"ok":True}

@app.delete("/api/groups/{gid}/members/{uid}",status_code=200)
def remove_group_member(gid:str,uid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    grp=db.query(ChatGroup).filter(ChatGroup.id==gid).first()
    if not grp: raise HTTPException(404)
    if uid!=u.id:
        if grp.created_by!=u.id and u.role!="owner": raise HTTPException(403)
    else:
        cnt=db.execute(_sa_text("SELECT COUNT(*) FROM chat_group_members WHERE group_id=:g").bindparams(g=gid)).scalar() or 0
        if cnt<=1: raise HTTPException(400,"Нельзя выйти из группы — вы последний участник")
    db.execute(_sa_text("DELETE FROM chat_group_members WHERE group_id=:g AND user_id=:u").bindparams(g=gid,u=uid))
    db.commit()
    return {"ok":True}

# ═══ PERSONAL BOARDS ═══
@app.get("/api/personal-boards",response_model=list[PersonalBoardListItem])
def list_personal_boards(u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    result=[]
    owned=db.query(PersonalBoard).filter(PersonalBoard.owner_id==u.id).order_by(PersonalBoard.updated_at.desc()).all()
    for b in owned:
        mc=db.execute(personal_board_share.select().where(personal_board_share.c.board_id==b.id)).fetchall()
        result.append({**b.__dict__,'is_owner':True,'owner_name':u.name,'member_count':len(mc)})
    shared_rows=db.execute(personal_board_share.select().where(personal_board_share.c.user_id==u.id)).fetchall()
    shared_ids=[r.board_id for r in shared_rows]
    if shared_ids:
        shared=db.query(PersonalBoard).filter(PersonalBoard.id.in_(shared_ids)).order_by(PersonalBoard.updated_at.desc()).all()
        for b in shared:
            owner=db.query(User).filter(User.id==b.owner_id).first()
            result.append({**b.__dict__,'is_owner':False,'owner_name':owner.name if owner else '?','member_count':0})
    return result

@app.post("/api/personal-boards",response_model=PersonalBoardOut,status_code=201)
def create_personal_board(d:PersonalBoardCreate,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    if _is_subscription_restricted(u): raise HTTPException(403,"Доски мастерской недоступны — требуется активная подписка")
    b=PersonalBoard(owner_id=u.id,title=d.title.strip()[:200] if d.title else "Новая доска")
    db.add(b); db.commit(); db.refresh(b); return b

@app.get("/api/personal-boards/{bid}",response_model=PersonalBoardOut)
def get_personal_board(bid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    b=db.query(PersonalBoard).filter(PersonalBoard.id==bid).first()
    if not b: raise HTTPException(404)
    if b.owner_id!=u.id:
        shared=db.execute(personal_board_share.select().where(
            (personal_board_share.c.board_id==bid)&(personal_board_share.c.user_id==u.id))).first()
        if not shared: raise HTTPException(403)
    return b

@app.patch("/api/personal-boards/{bid}",response_model=PersonalBoardOut)
def update_personal_board(bid:str,d:PersonalBoardUpdate,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    b=db.query(PersonalBoard).filter(PersonalBoard.id==bid,PersonalBoard.owner_id==u.id).first()
    if not b: raise HTTPException(404)
    if d.title is not None: b.title=d.title.strip()[:200]
    db.commit(); db.refresh(b); return b

@app.delete("/api/personal-boards/{bid}",status_code=204)
def delete_personal_board(bid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    b=db.query(PersonalBoard).filter(PersonalBoard.id==bid,PersonalBoard.owner_id==u.id).first()
    if not b: raise HTTPException(404)
    db.delete(b); db.commit()

@app.post("/api/personal-boards/{bid}/share",response_model=PersonalBoardOut)
def share_personal_board(bid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    b=db.query(PersonalBoard).filter(PersonalBoard.id==bid,PersonalBoard.owner_id==u.id).first()
    if not b: raise HTTPException(404)
    if not b.share_token: b.share_token=secrets.token_hex(16)
    db.commit(); db.refresh(b); return b

@app.delete("/api/personal-boards/{bid}/share",response_model=PersonalBoardOut)
def unshare_personal_board(bid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    b=db.query(PersonalBoard).filter(PersonalBoard.id==bid,PersonalBoard.owner_id==u.id).first()
    if not b: raise HTTPException(404)
    b.share_token=None; db.commit(); db.refresh(b); return b

@app.post("/api/personal-boards/{bid}/invite/{uid}",status_code=200)
def invite_to_board(bid:str,uid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    b=db.query(PersonalBoard).filter(PersonalBoard.id==bid,PersonalBoard.owner_id==u.id).first()
    if not b: raise HTTPException(404)
    if uid==u.id: raise HTTPException(400,"Нельзя пригласить себя")
    target=db.query(User).filter(User.id==uid).first()
    if not target: raise HTTPException(404,"Пользователь не найден")
    # already a member
    if db.execute(personal_board_share.select().where(
            (personal_board_share.c.board_id==bid)&(personal_board_share.c.user_id==uid))).first():
        return {"ok":True,"status":"already_member"}
    # pending invite already exists
    existing=db.query(BoardInvite).filter(BoardInvite.board_id==bid,BoardInvite.to_id==uid,BoardInvite.status=='pending').first()
    if existing: return {"ok":True,"status":"already_invited"}
    inv=BoardInvite(board_id=bid,from_id=u.id,to_id=uid)
    db.add(inv); db.flush()
    notif=Notification(user_id=uid,text=f"{u.name} приглашает вас на доску «{b.title}»",
                       link='/workshop.html',notif_type='board_invite')
    db.add(notif)
    # Send DM so the invite appears in ЛС
    dm_text=json.dumps({"type":"board_invite","invite_id":inv.id,"board_title":b.title,"board_id":bid},ensure_ascii=False)
    dm=Message(from_id=u.id,to_id=uid,text=dm_text)
    db.add(dm)
    db.commit()
    return {"ok":True,"status":"invited"}

@app.get("/api/board-invites/pending",response_model=list[BoardInviteOut])
def get_pending_invites(u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    invs=db.query(BoardInvite).filter(BoardInvite.to_id==u.id,BoardInvite.status=='pending').order_by(BoardInvite.created_at.desc()).all()
    result=[]
    for inv in invs:
        b=inv.board; fr=inv.from_user
        if b and fr:
            result.append({"id":inv.id,"board_id":inv.board_id,"board_title":b.title,
                           "from_id":inv.from_id,"from_name":fr.name,"created_at":inv.created_at})
    return result

@app.post("/api/board-invites/{iid}/accept",status_code=200)
def accept_board_invite(iid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    inv=db.query(BoardInvite).filter(BoardInvite.id==iid,BoardInvite.to_id==u.id,BoardInvite.status=='pending').first()
    if not inv: raise HTTPException(404)
    inv.status='accepted'
    if not db.execute(personal_board_share.select().where(
            (personal_board_share.c.board_id==inv.board_id)&(personal_board_share.c.user_id==u.id))).first():
        db.execute(personal_board_share.insert().values(board_id=inv.board_id,user_id=u.id))
    db.commit()
    return {"ok":True}

@app.post("/api/board-invites/{iid}/decline",status_code=200)
def decline_board_invite(iid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    inv=db.query(BoardInvite).filter(BoardInvite.id==iid,BoardInvite.to_id==u.id,BoardInvite.status=='pending').first()
    if not inv: raise HTTPException(404)
    inv.status='declined'; db.commit()
    return {"ok":True}

@app.get("/api/personal-boards/{bid}/members",response_model=list[BoardMemberOut])
def get_board_members(bid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    b=db.query(PersonalBoard).filter(PersonalBoard.id==bid,PersonalBoard.owner_id==u.id).first()
    if not b: raise HTTPException(403)
    rows=db.execute(personal_board_share.select().where(personal_board_share.c.board_id==bid)).fetchall()
    members=[]
    for r in rows:
        usr=db.query(User).filter(User.id==r.user_id).first()
        if usr: members.append({"id":usr.id,"name":usr.name,"role":usr.role.value if hasattr(usr.role,'value') else str(usr.role)})
    return members

@app.delete("/api/personal-boards/{bid}/members/{uid}",status_code=200)
def remove_board_member(bid:str,uid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    b=db.query(PersonalBoard).filter(PersonalBoard.id==bid,PersonalBoard.owner_id==u.id).first()
    if not b: raise HTTPException(403)
    db.execute(personal_board_share.delete().where(
        (personal_board_share.c.board_id==bid)&(personal_board_share.c.user_id==uid)))
    db.commit()
    return {"ok":True}

@app.delete("/api/personal-boards/{bid}/leave",status_code=200)
def leave_board(bid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    b=db.query(PersonalBoard).filter(PersonalBoard.id==bid).first()
    if not b: raise HTTPException(404)
    if b.owner_id==u.id: raise HTTPException(400,"Владелец не может покинуть свою доску")
    db.execute(personal_board_share.delete().where(
        (personal_board_share.c.board_id==bid)&(personal_board_share.c.user_id==u.id)))
    db.commit()
    return {"ok":True}

@app.get("/api/personal-boards/by-token/{token}",response_model=PersonalBoardOut)
def get_board_by_token(token:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    b=db.query(PersonalBoard).filter(PersonalBoard.share_token==token).first()
    if not b: raise HTTPException(404)
    if b.owner_id!=u.id:
        if not db.execute(personal_board_share.select().where(
                (personal_board_share.c.board_id==b.id)&(personal_board_share.c.user_id==u.id))).first():
            db.execute(personal_board_share.insert().values(board_id=b.id,user_id=u.id))
            db.commit()
    return b

# ═══ PERSONAL BOARD WEBSOCKET ═══
pb_conns: dict[str, dict[str,WebSocket]] = defaultdict(dict)  # bid -> {uid: ws}

async def _pb_bcast(bid,msg,exclude_ws=None):
    dead=[]
    for uid,c in list(pb_conns.get(bid,{}).items()):
        if c is exclude_ws: continue
        try: await c.send_text(msg)
        except: dead.append((bid,uid))
    for bid2,uid2 in dead:
        pb_conns[bid2].pop(uid2,None)



@app.websocket("/ws/board/group/{gid}")
async def group_board_ws(ws: WebSocket, gid: str):
    await ws.accept()
    token = ws.query_params.get("token")
    instance_id = ws.query_params.get("instance_id")
    if not token or not instance_id:
        await ws.close(code=4001); return
    try: payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except: await ws.close(code=4001); return
    uid = payload.get("sub"); urole = payload.get("role")
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == uid).first()
        if not user: await ws.close(code=4001); return
        g = db.query(Group).filter(Group.id == gid).first()
        if not g: await ws.close(code=4003); return
        # Доступ: репетитор группы, owner, teamlead команды или активный участник
        is_tutor_role = urole in ("tutor", "owner", "teamlead", "demo_tutor", "demo_teamlead")
        if is_tutor_role:
            try: _check_group_access(g, user, db)
            except: await ws.close(code=4003); return
        else:
            # Ученик — должен быть активным участником
            mem = db.query(GroupMembership).filter(
                GroupMembership.group_id == gid,
                GroupMembership.student_id == user.student_id,
                GroupMembership.left_at == None
            ).first() if user.student_id else None
            if not mem: await ws.close(code=4003); return
        _get_group_board(gid, instance_id, db)
        uname = user.name
    finally:
        db.close()

    room_key = f"group::{gid}::{instance_id}"
    brd_conns[room_key].add(ws)
    brd_users[room_key][uid] = uname
    await _bcast(room_key, json.dumps({"type": "user_join", "uid": uid, "name": uname,
        "online": [{"uid": k, "name": v} for k, v in brd_users[room_key].items()]}), ws)
    await ws.send_text(json.dumps({"type": "hello", "user_id": uid, "name": uname,
        "online": [{"uid": k, "name": v} for k, v in brd_users[room_key].items() if k != uid]}))
    try:
        while True:
            raw = await ws.receive_text()
            try: msg = json.loads(raw)
            except: continue
            mt = msg.get("type")
            if mt == "load":
                db = SessionLocal()
                try: b = _get_group_board(gid, instance_id, db); sj = b.strokes
                finally: db.close()
                await ws.send_text(json.dumps({"type": "strokes", "data": json.loads(sj)}))
            elif mt == "stroke":
                sd = msg.get("data", {})
                if not sd.get("user_id"): sd["user_id"] = uid
                db = SessionLocal()
                try:
                    b = _get_group_board(gid, instance_id, db)
                    c2 = json.loads(b.strokes); c2.append(sd); b.strokes = json.dumps(c2); db.commit()
                finally: db.close()
                await _bcast(room_key, json.dumps({"type": "stroke", "data": sd}), ws)
            elif mt == "clear":
                if urole not in ("owner", "tutor", "teamlead"): continue
                db = SessionLocal()
                try:
                    b = _get_group_board(gid, instance_id, db); b.strokes = "[]"; db.commit()
                finally: db.close()
                await _bcast(room_key, json.dumps({"type": "clear"}), ws)
            elif mt == "undo":
                db = SessionLocal(); rid = None
                try:
                    b = _get_group_board(gid, instance_id, db); c2 = json.loads(b.strokes)
                    for i in range(len(c2) - 1, -1, -1):
                        if c2[i].get("user_id") == uid: rid = c2[i].get("id"); c2.pop(i); break
                    if rid: b.strokes = json.dumps(c2); db.commit()
                finally: db.close()
                if rid:
                    await ws.send_text(json.dumps({"type": "erase_stroke", "id": rid}))
                    await _bcast(room_key, json.dumps({"type": "erase_stroke", "id": rid}), ws)
            elif mt == "erase_stroke":
                eid = msg.get("id")
                if not eid: continue
                db = SessionLocal()
                try:
                    b = _get_group_board(gid, instance_id, db); c2 = json.loads(b.strokes)
                    b.strokes = json.dumps([s for s in c2 if s.get("id") != eid]); db.commit()
                finally: db.close()
                await _bcast(room_key, json.dumps({"type": "erase_stroke", "id": eid}), ws)
            elif mt == "stroke_update":
                sd = msg.get("data", {}); eid = sd.get("id")
                if not eid: continue
                if not sd.get("user_id"): sd["user_id"] = uid
                db = SessionLocal()
                try:
                    b = _get_group_board(gid, instance_id, db); c2 = json.loads(b.strokes)
                    idx = next((i for i, s in enumerate(c2) if s.get("id") == eid), -1)
                    if idx >= 0: c2[idx] = sd
                    else: c2.append(sd)
                    b.strokes = json.dumps(c2); db.commit()
                finally: db.close()
                await _bcast(room_key, json.dumps({"type": "stroke_update", "data": sd}), ws)
            elif mt in ("cursor", "view"):
                msg["uid"] = uid; msg["name"] = uname
                await _bcast(room_key, json.dumps(msg), ws)
    except WebSocketDisconnect: pass
    except Exception as e: print(f"[GROUP_WS] {e}")
    finally:
        brd_conns[room_key].discard(ws)
        brd_users[room_key].pop(uid, None)
        if not brd_conns.get(room_key): brd_users.pop(room_key, None)
        await _bcast(room_key, json.dumps({"type": "user_leave", "uid": uid}), None)
@app.websocket("/ws/personal-board/{bid}")
async def personal_board_ws(ws:WebSocket,bid:str):
    await ws.accept()
    token=ws.query_params.get("token")
    if not token: await ws.close(code=4001); return
    try: payload=jwt.decode(token,SECRET_KEY,algorithms=[ALGORITHM])
    except: await ws.close(code=4001); return
    uid=payload.get("sub"); uname_ws=payload.get("name","")
    db=SessionLocal()
    try:
        user=db.query(User).filter(User.id==uid).first()
        if not user: await ws.close(code=4001); return
        b=db.query(PersonalBoard).filter(PersonalBoard.id==bid).first()
        if not b: await ws.close(code=4004); return
        if b.owner_id!=uid:
            shared=db.execute(personal_board_share.select().where(
                (personal_board_share.c.board_id==bid)&(personal_board_share.c.user_id==uid))).first()
            if not shared: await ws.close(code=4003); return
        uname_ws=user.name
    finally: db.close()
    pb_conns[bid][uid]=ws
    # notify others about join
    await _pb_bcast(bid,json.dumps({"type":"user_join","uid":uid,"name":uname_ws}),ws)
    # send current users list
    online=[{"uid":k,"name":"?"} for k in pb_conns[bid] if k!=uid]
    await ws.send_text(json.dumps({"type":"hello","user_id":uid,"online":online}))
    try:
        while True:
            raw=await ws.receive_text()
            try: msg=json.loads(raw)
            except: continue
            mt=msg.get("type")
            if mt=="load":
                db=SessionLocal()
                try: b=db.query(PersonalBoard).filter(PersonalBoard.id==bid).first(); sj=b.strokes if b else "[]"
                finally: db.close()
                await ws.send_text(json.dumps({"type":"strokes","data":json.loads(sj)}))
            elif mt=="stroke":
                sd=msg.get("data",{}); sd["user_id"]=uid
                db=SessionLocal()
                try:
                    b=db.query(PersonalBoard).filter(PersonalBoard.id==bid).first()
                    if b: c=json.loads(b.strokes); c.append(sd); b.strokes=json.dumps(c); db.commit()
                finally: db.close()
                await _pb_bcast(bid,json.dumps({"type":"stroke","data":sd}),ws)
            elif mt=="erase_stroke":
                eid=msg.get("id")
                if not eid: continue
                db=SessionLocal()
                try:
                    b=db.query(PersonalBoard).filter(PersonalBoard.id==bid).first()
                    if b: b.strokes=json.dumps([s for s in json.loads(b.strokes) if s.get("id")!=eid]); db.commit()
                finally: db.close()
                await _pb_bcast(bid,json.dumps({"type":"erase_stroke","id":eid}),ws)
            elif mt=="stroke_update":
                sd=msg.get("data",{}); eid=sd.get("id")
                if not eid: continue
                if not sd.get("user_id"): sd["user_id"]=uid
                db=SessionLocal()
                try:
                    b=db.query(PersonalBoard).filter(PersonalBoard.id==bid).first()
                    if b:
                        c=json.loads(b.strokes)
                        idx=next((i for i,s in enumerate(c) if s.get("id")==eid),-1)
                        if idx>=0: c[idx]=sd
                        else: c.append(sd)
                        b.strokes=json.dumps(c); db.commit()
                finally: db.close()
                await _pb_bcast(bid,json.dumps({"type":"stroke_update","data":sd}),ws)
            elif mt=="clear":
                db=SessionLocal()
                try:
                    b=db.query(PersonalBoard).filter(PersonalBoard.id==bid).first()
                    if b: b.strokes="[]"; db.commit()
                finally: db.close()
                await _pb_bcast(bid,json.dumps({"type":"clear"}),ws)
            elif mt in ("cursor","view"):
                msg["uid"]=uid; msg["name"]=uname_ws
                await _pb_bcast(bid,json.dumps(msg),ws)
    except WebSocketDisconnect: pass
    except Exception as e: print(f"[PB_WS] {e}")
    finally:
        pb_conns[bid].pop(uid,None)
        if not pb_conns[bid]: del pb_conns[bid]
        await _pb_bcast(bid,json.dumps({"type":"user_leave","uid":uid}))

# ═══ SCHEDULE ═══
@app.get("/api/schedule")
def get_schedule(u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db),tutor_id:str=None):
    target_id=u.id
    if tutor_id and tutor_id!=u.id:
        if u.role=="owner": target_id=tutor_id
        elif u.role=="teamlead":
            tids=_team_tutor_ids(u.id,db)
            if tutor_id not in tids: raise HTTPException(403,"Тьютор не в вашей команде")
            target_id=tutor_id
        else: raise HTTPException(403)
    slots=db.query(ScheduleSlot).options(joinedload(ScheduleSlot.student),joinedload(ScheduleSlot.instance),joinedload(ScheduleSlot.group)).filter(ScheduleSlot.tutor_id==target_id).all()
    return [{"id":s.id,"tutor_id":s.tutor_id,"student_id":s.student_id,
             "student_name":s.student.name if s.student else None,
             "day_of_week":s.day_of_week,"slot_index":s.slot_index,
             "duration":s.duration,"note":s.note,"color":s.color,
             "instance_id":s.instance_id,
             "instance_title":s.instance.title if s.instance else None,
             "group_id":s.group_id,
             "group_name":s.group.name if s.group else None,
             "group_member_count":db.query(GroupMembership).filter(GroupMembership.group_id==s.group_id,GroupMembership.left_at==None).count() if s.group_id else None} for s in slots]

@app.post("/api/schedule",status_code=200)
def set_schedule_slot(d:ScheduleSlotCreate,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    if d.day_of_week not in range(7) or d.slot_index not in range(48): raise HTTPException(400,"Некорректные данные")
    if d.student_id and d.group_id: raise HTTPException(400,"Слот не может быть одновременно индивидуальным и групповым")
    # Проверяем, что ученик принадлежит этому репетитору
    if d.student_id:
        _st=db.query(Student).filter(Student.id==d.student_id).first()
        if not _st: raise HTTPException(404,"Ученик не найден")
        _is_own=_st.created_by==u.id
        _is_link=db.execute(tutor_student_link.select().where(
            (tutor_student_link.c.tutor_id==u.id)&(tutor_student_link.c.student_id==d.student_id)
        )).first() is not None
        if not _is_own and not _is_link:
            raise HTTPException(403,"Этот ученик не закреплён за вами")
    if d.group_id:
        _g=db.query(Group).filter(Group.id==d.group_id).first()
        if not _g: raise HTTPException(404,"Группа не найдена")
        _check_group_access(_g,u,db)
    existing=db.query(ScheduleSlot).filter(
        ScheduleSlot.tutor_id==u.id,ScheduleSlot.day_of_week==d.day_of_week,ScheduleSlot.slot_index==d.slot_index
    ).first()
    dur=max(1,min(d.duration,8))
    if existing:
        existing.student_id=d.student_id; existing.group_id=d.group_id
        existing.instance_id=getattr(d,"instance_id",None); existing.duration=dur; existing.note=d.note; existing.color=d.color
        db.commit(); db.refresh(existing); slot=existing
    else:
        slot=ScheduleSlot(tutor_id=u.id,student_id=d.student_id,group_id=d.group_id,
                          instance_id=getattr(d,"instance_id",None),day_of_week=d.day_of_week,
                          slot_index=d.slot_index,duration=dur,note=d.note,color=d.color)
        db.add(slot); db.commit(); db.refresh(slot)
    st=db.query(Student).filter(Student.id==slot.student_id).first() if slot.student_id else None
    grp=db.query(Group).filter(Group.id==slot.group_id).first() if slot.group_id else None
    return {"id":slot.id,"tutor_id":slot.tutor_id,"student_id":slot.student_id,
            "student_name":st.name if st else None,
            "group_id":slot.group_id,"group_name":grp.name if grp else None,
            "day_of_week":slot.day_of_week,"slot_index":slot.slot_index,
            "duration":slot.duration,"note":slot.note,"color":slot.color,
            "instance_id":slot.instance_id,"instance_title":slot.instance.title if slot.instance else None}

@app.delete("/api/schedule/{slot_id}",status_code=204)
def del_schedule_slot(slot_id:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    slot=db.query(ScheduleSlot).filter(ScheduleSlot.id==slot_id,ScheduleSlot.tutor_id==u.id).first()
    if not slot: raise HTTPException(404)
    db.delete(slot); db.commit()

@app.get("/api/schedule/my")
def get_my_schedule(u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    if u.role!="student" or not u.student_id: raise HTTPException(403)
    # Групповые слоты: найдём active group_ids для ученика
    active_gids=[m.group_id for m in db.query(GroupMembership).filter(
        GroupMembership.student_id==u.student_id,GroupMembership.left_at==None).all()]
    from sqlalchemy import or_ as _or
    _filter=_or(ScheduleSlot.student_id==u.student_id,
               ScheduleSlot.group_id.in_(active_gids)) if active_gids else ScheduleSlot.student_id==u.student_id
    slots=db.query(ScheduleSlot).options(
        joinedload(ScheduleSlot.student),joinedload(ScheduleSlot.group)).filter(
        _filter).order_by(ScheduleSlot.day_of_week,ScheduleSlot.slot_index).all()
    return [{"id":s.id,"tutor_id":s.tutor_id,"student_id":s.student_id,
             "day_of_week":s.day_of_week,"slot_index":s.slot_index,"duration":s.duration,
             "note":s.note,"student_note":s.student_note,"color":s.color,
             "group_id":s.group_id,"group_name":s.group.name if s.group else None,
             "instance_id":s.instance_id} for s in slots]

@app.patch("/api/schedule/{slot_id}/student-note",status_code=200)
def set_student_note(slot_id:str,d:dict=Body(...),u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    if u.role!="student" or not u.student_id: raise HTTPException(403)
    slot=db.query(ScheduleSlot).filter(ScheduleSlot.id==slot_id,ScheduleSlot.student_id==u.student_id).first()
    if not slot: raise HTTPException(404)
    slot.student_note=d.get("student_note","")[:500]
    db.commit(); return {"ok":True}

# ═══ STUDENT COURSES ═══

@app.get("/api/schedule/parent")
def get_parent_schedule(u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    if u.role!="parent": raise HTTPException(403)
    child_ids=[r.student_id for r in db.execute(parent_student_link.select().where(parent_student_link.c.parent_id==u.id)).fetchall()]
    if not child_ids: return []
    # Групповые слоты для детей
    child_gids=list({m.group_id for cid in child_ids for m in db.query(GroupMembership).filter(
        GroupMembership.student_id==cid,GroupMembership.left_at==None).all()})
    from sqlalchemy import or_ as _or
    _pf=_or(ScheduleSlot.student_id.in_(child_ids),
            ScheduleSlot.group_id.in_(child_gids)) if child_gids else ScheduleSlot.student_id.in_(child_ids)
    slots=db.query(ScheduleSlot).options(joinedload(ScheduleSlot.group)).filter(
        _pf).order_by(ScheduleSlot.day_of_week,ScheduleSlot.slot_index).all()
    students={s.id:s for s in db.query(Student).filter(Student.id.in_(child_ids)).all()}
    return [{"id":s.id,"tutor_id":s.tutor_id,"student_id":s.student_id,
             "student_name":students[s.student_id].name if s.student_id in students else None,
             "day_of_week":s.day_of_week,"slot_index":s.slot_index,"duration":s.duration,
             "note":s.note,"student_note":s.student_note,"color":s.color,
             "group_id":s.group_id,"group_name":s.group.name if s.group else None,
             "instance_id":s.instance_id} for s in slots]

@app.get("/api/students/{stid}/courses")
def get_student_courses(stid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    chk_acc(stid,u,db)
    courses=db.query(StudentCourse).filter(StudentCourse.student_id==stid).order_by(StudentCourse.created_at).all()
    result=[]
    for c in courses:
        tutor=db.query(User).filter(User.id==c.tutor_id).first() if c.tutor_id else None
        secs=db.query(Section).filter(Section.course_id==c.id).order_by(Section.position).all()
        result.append({"id":c.id,"student_id":c.student_id,"title":c.title,
                       "tutor_id":c.tutor_id,"tutor_name":tutor.name if tutor else None,
                       "sections":[{"id":s.id,"student_id":s.student_id,"course_id":s.course_id,
                                    "title":s.title,"position":s.position,"is_open":s.is_open,
                                    "idz_enabled":s.idz_enabled,"control_enabled":s.control_enabled,
                                    "idz":s.idz,"control":s.control.value if s.control else "none",
                                    "locked":s.locked,"idz_text":s.idz_text,
                                    "items":[{"id":i.id,"section_id":i.section_id,"type":i.type.value if i.type else i.type,
                                              "position":i.position,"name":i.name,"status":i.status.value if i.status else "none",
                                              "total":i.total,"done":i.done,"grade":i.grade,"note":i.note,
                                              "student_answer":i.student_answer,"text":i.text,
                                              "attachments":[{"id":a.id,"item_id":a.item_id,"name":a.name,
                                                              "mime":a.mime,"size":a.size,"file_path":a.file_path} for a in i.attachments],
                                              "subblocks":[{"id":sb.id,"item_id":sb.item_id,"type":sb.type,
                                                            "content":sb.content,"position":sb.position} for sb in i.subblocks]
                                             } for i in s.items]} for s in secs]})
    return result

@app.post("/api/students/{stid}/courses",status_code=201)
def create_student_course(stid:str,d:StudentCourseCreate,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    chk_acc(stid,u,db)
    if not db.query(Student).filter(Student.id==stid).first(): raise HTTPException(404)
    if d.tutor_id:
        t=db.query(User).filter(User.id==d.tutor_id,User.role.in_(["tutor","owner"])).first()
        if not t: raise HTTPException(404,"Преподаватель не найден")
    c=StudentCourse(student_id=stid,tutor_id=d.tutor_id,title=d.title.strip()[:200])
    db.add(c); db.commit(); db.refresh(c)
    tutor=db.query(User).filter(User.id==c.tutor_id).first() if c.tutor_id else None
    return {"id":c.id,"student_id":c.student_id,"title":c.title,
            "tutor_id":c.tutor_id,"tutor_name":tutor.name if tutor else None,"sections":[]}

@app.patch("/api/student-courses/{cid}",status_code=200)
def upd_student_course(cid:str,d:StudentCourseUpdate,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    c=db.query(StudentCourse).filter(StudentCourse.id==cid).first()
    if not c: raise HTTPException(404)
    chk_acc(c.student_id,u,db)
    if d.title is not None: c.title=d.title.strip()[:200]
    if d.tutor_id is not None: c.tutor_id=d.tutor_id or None
    db.commit(); db.refresh(c)
    tutor=db.query(User).filter(User.id==c.tutor_id).first() if c.tutor_id else None
    return {"id":c.id,"student_id":c.student_id,"title":c.title,
            "tutor_id":c.tutor_id,"tutor_name":tutor.name if tutor else None}

@app.delete("/api/student-courses/{cid}",status_code=204)
def del_student_course(cid:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    c=db.query(StudentCourse).filter(StudentCourse.id==cid).first()
    if not c: raise HTTPException(404)
    chk_acc(c.student_id,u,db)
    db.delete(c); db.commit()

# ═══ ПРОГРАММЫ (course_instances) ═══

def _instance_out(ci, db, with_sections=False):
    tutor=db.query(User).filter(User.id==ci.tutor_id).first() if ci.tutor_id else None
    subj=db.query(Subject).filter(Subject.id==ci.subject_id).first() if ci.subject_id else None
    secs=[]
    if with_sections:
        from sqlalchemy import and_
        raw_secs=db.query(Section).filter(Section.instance_id==ci.id).order_by(Section.position).all()
        for sec in raw_secs:
            items_out=[]
            for it in sec.items:
                atts=[{"id":a.id,"item_id":a.item_id,"name":a.name,"mime":a.mime,"size":a.size,"file_path":a.file_path} for a in it.attachments]
                sbs=[{"id":sb.id,"item_id":sb.item_id,"type":sb.type,"content":sb.content,"name":sb.name,"position":sb.position,"file_path":sb.file_path,"mime":sb.mime,"size":sb.size} for sb in it.subblocks]
                items_out.append({"id":it.id,"section_id":it.section_id,"type":it.type,"position":it.position,
                    "name":it.name,"status":it.status,"total":it.total,"done":it.done,"closed":it.closed,
                    "date":it.date,"closed_date":it.closed_date,"note":it.note,"text":it.text,
                    "grade":it.grade,"student_answer":it.student_answer,"lang":it.lang,
                    "attachments":atts,"subblocks":sbs})
            secs.append({"id":sec.id,"student_id":sec.student_id,"instance_id":sec.instance_id,
                "title":sec.title,"position":sec.position,"is_open":sec.is_open,
                "idz_enabled":sec.idz_enabled,"control_enabled":sec.control_enabled,
                "idz":sec.idz,"control":sec.control,"locked":sec.locked,"idz_text":sec.idz_text,
                "course_id":sec.course_id,"items":items_out})
    return {"id":ci.id,"title":ci.title,
            "tutor_id":ci.tutor_id,"tutor_name":tutor.name if tutor else None,
            "course_id":ci.course_id,
            "subject_id":ci.subject_id,"subject_name":subj.name if subj else None,
            "grade":ci.grade,"goal":ci.goal,
            "created_at":ci.created_at.isoformat() if ci.created_at else None,
            "sections":secs}

@app.get("/api/students/{sid}/instances")
def list_instances(sid:str, u:User=Depends(require_tutor_or_owner), db:Session=Depends(get_db)):
    """Список программ ученика."""
    chk_acc(sid, u, db)
    enrollments=db.query(Enrollment).filter(Enrollment.student_id==sid).order_by(Enrollment.created_at).all()
    result=[]
    for e in enrollments:
        ci=db.query(CourseInstance).filter(CourseInstance.id==e.instance_id).first()
        if not ci: continue
        secs_count=db.query(Section).filter(Section.instance_id==ci.id).count()
        tutor=db.query(User).filter(User.id==ci.tutor_id).first() if ci.tutor_id else None
        subj=db.query(Subject).filter(Subject.id==ci.subject_id).first() if ci.subject_id else None
        result.append({"id":ci.id,"title":ci.title,
            "tutor_id":ci.tutor_id,"tutor_name":tutor.name if tutor else None,
            "subject_id":ci.subject_id,"subject_name":subj.name if subj else None,
            "grade":ci.grade,"goal":ci.goal,
            "sections_count":secs_count,"created_at":ci.created_at.isoformat() if ci.created_at else None,
            "enrollment_id":e.id})
    return result

@app.get("/api/instances/{iid}")
def get_instance(iid:str, u:User=Depends(get_current_user), db:Session=Depends(get_db)):
    """Получить программу с секциями."""
    ci=db.query(CourseInstance).filter(CourseInstance.id==iid).first()
    if not ci: raise HTTPException(404)
    # Проверка доступа: найти enrollment и проверить студента
    if u.role not in ("owner","tutor","teamlead"):
        enr=db.query(Enrollment).filter(Enrollment.instance_id==iid).first()
        if not enr: raise HTTPException(403)
        if u.role=="student" and u.student_id!=enr.student_id: raise HTTPException(403)
        if u.role=="parent":
            sids=[r.student_id for r in db.execute(parent_student_link.select().where(parent_student_link.c.parent_id==u.id)).fetchall()]
            if enr.student_id not in sids: raise HTTPException(403)
    return _instance_out(ci, db, with_sections=True)

@app.post("/api/students/{sid}/instances", status_code=201)
def create_instance(sid:str, d:CourseInstanceCreate, u:User=Depends(require_tutor_or_owner), db:Session=Depends(get_db)):
    """Создать новую программу для ученика."""
    chk_acc(sid, u, db)
    tutor_id=d.tutor_id or (u.id if u.role in ("tutor","teamlead") else None)
    ci=CourseInstance(id=gen_id(), title=d.title.strip() or "Программа",
        tutor_id=tutor_id, course_id=d.course_id, subject_id=d.subject_id,
        grade=d.grade, goal=d.goal,
        study_plan_id=getattr(d,'study_plan_id',None))
    db.add(ci); db.flush()
    enr=Enrollment(id=gen_id(), instance_id=ci.id, student_id=sid)
    db.add(enr)
    # Если указан шаблон курса — копируем секции
    if d.course_id:
        tmpl=db.query(Course).filter(Course.id==d.course_id).first()
        if tmpl:
            for pos,cs in enumerate(tmpl.sections):
                sec=Section(id=gen_id(), student_id=sid, instance_id=ci.id,
                    title=cs.title, position=pos,
                    idz_enabled=cs.idz_enabled, control_enabled=cs.control_enabled, idz_text=cs.idz_text)
                db.add(sec); db.flush()
                for ipos,titem in enumerate(cs.items):
                    it=Item(id=gen_id(), section_id=sec.id, type=titem.type,
                        position=ipos, name=titem.name, total=titem.total, text=titem.text)
                    db.add(it)
    db.commit()
    return _instance_out(ci, db)

@app.patch("/api/instances/{iid}")
def update_instance(iid:str, d:CourseInstanceUpdate, u:User=Depends(require_tutor_or_owner), db:Session=Depends(get_db)):
    ci=db.query(CourseInstance).filter(CourseInstance.id==iid).first()
    if not ci: raise HTTPException(404)
    if d.title is not None: ci.title=d.title.strip() or ci.title
    if d.tutor_id is not None: ci.tutor_id=d.tutor_id or None
    if d.subject_id is not None: ci.subject_id=d.subject_id or None
    if d.grade is not None: ci.grade=d.grade or None
    if d.goal is not None: ci.goal=d.goal or None
    db.commit()
    return _instance_out(ci, db)

@app.delete("/api/instances/{iid}", status_code=204)
def delete_instance(iid:str, u:User=Depends(require_tutor_or_owner), db:Session=Depends(get_db)):
    ci=db.query(CourseInstance).filter(CourseInstance.id==iid).first()
    if not ci: raise HTTPException(404)
    # Проверка: тьютор может удалять только свои программы
    if u.role=="tutor" and ci.tutor_id!=u.id: raise HTTPException(403)
    db.delete(ci); db.commit()


@app.post("/api/instances/{iid}/sections", response_model=SectionOut, status_code=201)
def create_instance_sec(iid:str, d:SectionCreate, u:User=Depends(require_tutor_or_owner), db:Session=Depends(get_db)):
    ci=db.query(CourseInstance).filter(CourseInstance.id==iid).first()
    if not ci: raise HTTPException(404)
    enr=db.query(Enrollment).filter(Enrollment.instance_id==iid).first()
    sid=enr.student_id if enr else None
    if not sid: raise HTTPException(400,"Нет студентов в программе")
    mp=db.query(Section.position).filter(Section.instance_id==iid).order_by(Section.position.desc()).first()
    sec=Section(id=gen_id(), student_id=sid, instance_id=iid,
                title=d.title, position=(mp[0]+1) if mp else 0,
                idz_enabled=d.idz_enabled, control_enabled=d.control_enabled, idz_text=d.idz_text)
    db.add(sec); db.commit(); db.refresh(sec); return sec


@app.post("/api/instances/{iid}/apply-course/{cid}")
def apply_course_instance(iid:str,cid:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    ci=db.query(CourseInstance).filter(CourseInstance.id==iid).first()
    if not ci: raise HTTPException(404)
    co=db.query(Course).options(
        joinedload(Course.sections).joinedload(CourseSection.items).joinedload(CourseSectionItem.subblocks)
    ).filter(Course.id==cid).first()
    if not co: raise HTTPException(404)
    if co.access=="private" and co.author_id!=u.id and u.role!="owner": raise HTTPException(403)
    enr=db.query(Enrollment).filter(Enrollment.instance_id==iid).first()
    sid=enr.student_id if enr else None
    for sec in db.query(Section).filter(Section.instance_id==iid).all(): _cln_sec(sec,db)
    db.query(Section).filter(Section.instance_id==iid).delete()
    for csec in co.sections:
        s=Section(id=gen_id(),student_id=sid,instance_id=iid,title=csec.title,position=csec.position,
                  idz_enabled=csec.idz_enabled,control_enabled=csec.control_enabled,idz_text=csec.idz_text)
        db.add(s); db.flush()
        for ci2 in csec.items:
            ni=Item(id=gen_id(),section_id=s.id,type=ci2.type,position=ci2.position,name=ci2.name or "",total=ci2.total,text=ci2.text,status="none")
            db.add(ni); db.flush()
            if ci2.type=='media' and ci2.file_path:
                db.add(Attachment(item_id=ni.id,name=ci2.name or "file",mime=ci2.mime or "application/octet-stream",size=ci2.size or 0,file_path=ci2.file_path))
            for sb in ci2.subblocks:
                db.add(ItemSubblock(item_id=ni.id,type=sb.type,content=sb.content,name=sb.name,position=sb.position,file_path=sb.file_path,mime=sb.mime,size=sb.size))
    db.commit()
    return _instance_out(ci,db,with_sections=True)

@app.post("/api/instances/{iid}/apply-template/{tkey}")
def apply_tpl_instance(iid:str,tkey:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    ci=db.query(CourseInstance).filter(CourseInstance.id==iid).first()
    if not ci: raise HTTPException(404)
    if tkey not in TPL: raise HTTPException(400)
    enr=db.query(Enrollment).filter(Enrollment.instance_id==iid).first()
    sid=enr.student_id if enr else None
    for sec in db.query(Section).filter(Section.instance_id==iid).all(): _cln_sec(sec,db)
    db.query(Section).filter(Section.instance_id==iid).delete()
    for pos,(title,idz,ctrl,items) in enumerate(TPL[tkey]):
        sec=Section(id=gen_id(),student_id=sid,instance_id=iid,title=title,position=pos,idz_enabled=bool(idz),control_enabled=bool(ctrl))
        db.add(sec); db.flush()
        for ip,nm in enumerate(items): db.add(Item(id=gen_id(),section_id=sec.id,type="topic",position=ip,name=nm,status="none"))
    db.commit()
    return _instance_out(ci,db,with_sections=True)

@app.post("/api/instances/{iid}/save-as-course",response_model=CourseOut,status_code=201)
def save_instance_as_course(iid:str,d:SaveAsCourseRequest,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    if not db.query(Subject).filter(Subject.id==d.subject_id).first(): raise HTTPException(404,"Предмет не найден")
    if d.replace_id:
        old=db.query(Course).filter(Course.id==d.replace_id).first()
        if old and (old.author_id==u.id or u.role=="owner"): db.delete(old); db.flush()
    c=Course(id=gen_id(),subject_id=d.subject_id,author_id=u.id,title=d.title,access=d.access)
    db.add(c); db.flush()
    secs=db.query(Section).filter(Section.instance_id==iid).order_by(Section.position).all()
    for sec in secs:
        csec=CourseSection(id=gen_id(),course_id=c.id,title=sec.title,position=sec.position,
                           idz_enabled=sec.idz_enabled,control_enabled=sec.control_enabled,idz_text=sec.idz_text)
        db.add(csec); db.flush()
        pos=0
        for item in sorted(db.query(Item).filter(Item.section_id==sec.id).all(),key=lambda i:i.position):
            if item.type in ("topic","hw","media","note"):
                nci=CourseSectionItem(id=gen_id(),section_id=csec.id,type=item.type,position=pos,name=item.name or "",total=item.total,text=item.text)
                db.add(nci); db.flush()
                for sb in db.query(ItemSubblock).filter(ItemSubblock.item_id==item.id).all():
                    db.add(CourseItemSubblock(item_id=nci.id,type=sb.type,content=sb.content,name=sb.name,position=sb.position,file_path=sb.file_path,mime=sb.mime,size=sb.size))
                pos+=1
    db.commit(); db.refresh(c)
    return db.query(Course).options(joinedload(Course.sections).joinedload(CourseSection.items)).filter(Course.id==c.id).first()

# ═══ TEAMLEAD API ═══

@app.get("/api/teamlead/team")
def get_team(u:User=Depends(require_teamlead_or_owner),db:Session=Depends(get_db)):
    """Список тьюторов команды teamlead с их студентами."""
    if u.role=="owner":
        tutors=db.query(User).filter(User.role=="tutor",User.teamlead_id!=None).order_by(User.created_at).all()
    else:
        tutors=db.query(User).filter(User.teamlead_id==u.id,User.role=="tutor").order_by(User.created_at).all()
    result=[]
    for t in tutors:
        stids=_tutor_student_ids(t.id,db)
        students=db.query(Student).filter(Student.id.in_(stids)).all() if stids else []
        result.append({"id":t.id,"name":t.name,"login":t.login,"role":t.role,
                       "last_seen":t.last_seen.isoformat() if t.last_seen else None,
                       "subject_id":t.subject_id,"teamlead_id":t.teamlead_id,
                       "students":[{"id":s.id,"name":s.name,"base_rate":s.base_rate} for s in students]})
    return result

@app.patch("/api/teamlead/team/{uid}/credentials")
def update_team_credentials(uid:str,d:dict=Body(...),u:User=Depends(require_teamlead_or_owner),db:Session=Depends(get_db)):
    """Teamlead меняет логин/пароль члена своей команды без change-request."""
    t=db.query(User).filter(User.id==uid).first()
    if not t: raise HTTPException(404)
    # Teamlead может менять только членов своей команды
    if u.role=="teamlead" and t.teamlead_id!=u.id and t.id!=u.id:
        raise HTTPException(403,"Нет доступа")
    if "login" in d and d["login"]:
        new_login=str(d["login"]).strip()
        if db.query(User).filter(User.login==new_login,User.id!=t.id).first(): raise HTTPException(409,"Логин занят")
        t.login=new_login
    if "password" in d and d["password"]:
        new_pw=str(d["password"])
        _check_password(new_pw)
        t.password_hash=hash_password(new_pw); t.must_change_password=False
    db.commit()
    return {"ok":True}

@app.get("/api/teamlead/subscription")
def get_subscription(u:User=Depends(require_teamlead_or_owner),db:Session=Depends(get_db)):
    """Текущая подписка teamlead."""
    tid=u.id if u.role=="teamlead" else None
    if not tid: return {"is_active":True,"plan":"owner","days_left":9999}
    sub=db.query(TeamLeadSubscription).filter(
        TeamLeadSubscription.teamlead_id==tid,
        TeamLeadSubscription.is_active==True
    ).order_by(TeamLeadSubscription.ends_at.desc()).first()
    now=datetime.now(timezone.utc)
    if not sub: return {"is_active":False,"plan":None,"days_left":0}
    days=(sub.ends_at.replace(tzinfo=timezone.utc)-now).days
    return {"id":sub.id,"is_active":days>0,"plan":sub.plan,"starts_at":sub.starts_at.isoformat(),
            "ends_at":sub.ends_at.isoformat(),"days_left":max(0,days)}

@app.post("/api/teamlead/subscription",status_code=201)
def create_subscription(d:dict=Body(...),o:User=Depends(require_owner),db:Session=Depends(get_db)):
    """Owner создаёт/продлевает подписку для teamlead."""
    tl=db.query(User).filter(User.id==d.get("teamlead_id"),User.role=="teamlead").first()
    if not tl: raise HTTPException(404,"Teamlead не найден")
    from datetime import datetime as _dt
    starts=_dt.fromisoformat(d.get("starts_at",datetime.now(timezone.utc).isoformat()))
    ends=_dt.fromisoformat(d.get("ends_at"))
    sub=TeamLeadSubscription(id=gen_id(),teamlead_id=tl.id,starts_at=starts,ends_at=ends,
                              plan=d.get("plan","monthly"),price=int(d.get("price",0)))
    db.add(sub); db.commit(); db.refresh(sub)
    return {"id":sub.id,"ok":True}

# === OWNER SUBSCRIPTION MANAGEMENT ===

@app.get("/api/owner/pending-subscriptions")
def get_pending_subscriptions(u:User=Depends(require_owner),db:Session=Depends(get_db)):
    users=db.query(User).filter(User.subscription_status.in_(["pending_approval","pending_payment","expired"])).order_by(User.created_at.desc()).all()
    now=datetime.now(timezone.utc)
    result=[]
    for usr in users:
        days_overdue=None
        if usr.trial_ends_at:
            te=usr.trial_ends_at.replace(tzinfo=timezone.utc) if usr.trial_ends_at.tzinfo is None else usr.trial_ends_at
            days_overdue=max(0,(now-te).days)
        result.append({"id":usr.id,"name":usr.name,"login":usr.login,"email":usr.email,
            "role":usr.role.value,"subscription_model":usr.subscription_model.value if usr.subscription_model else None,
            "subscription_status":usr.subscription_status.value if usr.subscription_status else None,
            "trial_ends_at":usr.trial_ends_at.isoformat() if usr.trial_ends_at else None,
            "days_overdue":days_overdue,"created_at":usr.created_at.isoformat() if usr.created_at else None})
    return result

@app.get("/api/owner/all-subscriptions")
def get_all_subscriptions(u:User=Depends(require_owner),db:Session=Depends(get_db)):
    users=db.query(User).filter(User.subscription_status.isnot(None)).order_by(User.created_at.desc()).all()
    now=datetime.now(timezone.utc)
    result=[]
    for usr in users:
        days_left=None
        if usr.trial_ends_at and usr.subscription_status=="trial":
            te=usr.trial_ends_at.replace(tzinfo=timezone.utc) if usr.trial_ends_at.tzinfo is None else usr.trial_ends_at
            days_left=max(0,(te-now).days)
        result.append({"id":usr.id,"name":usr.name,"login":usr.login,"email":usr.email,
            "role":usr.role.value,"subscription_model":usr.subscription_model.value if usr.subscription_model else None,
            "subscription_status":usr.subscription_status.value if usr.subscription_status else None,
            "trial_ends_at":usr.trial_ends_at.isoformat() if usr.trial_ends_at else None,
            "days_left":days_left,"created_at":usr.created_at.isoformat() if usr.created_at else None})
    return result

@app.post("/api/owner/users/{uid}/approve-commission")
def approve_commission(uid:str,u:User=Depends(require_owner),db:Session=Depends(get_db)):
    t=db.query(User).filter(User.id==uid).first()
    if not t: raise HTTPException(404,"Пользователь не найден")
    if t.subscription_model!="percent": raise HTTPException(400,"Пользователь не на комиссионном плане")
    t.subscription_status="active"; t.commission_approved_by=u.id; t.commission_approved_at=datetime.now(timezone.utc)
    db.add(Notification(user_id=t.id,text="Ваш аккаунт одобрен. Работайте в штатном режиме — комиссия 5% начисляется с каждого занятия.",notif_type="subscription_approved",link="/hbm_tutor.html"))
    db.commit(); return {"ok":True}

@app.post("/api/owner/users/{uid}/confirm-payment")
def owner_confirm_payment(uid:str,d:dict=Body(...),u:User=Depends(require_owner),db:Session=Depends(get_db)):
    t=db.query(User).filter(User.id==uid).first()
    if not t: raise HTTPException(404,"Пользователь не найден")
    from datetime import timedelta as _td
    now=datetime.now(timezone.utc)
    sub=db.query(TutorSubscription).filter(TutorSubscription.user_id==uid,TutorSubscription.is_active==True).first()
    if sub:
        base=sub.ends_at.replace(tzinfo=timezone.utc) if sub.ends_at and sub.ends_at.tzinfo is None else (sub.ends_at or now)
        sub.ends_at=(base if base>now else now)+_td(days=30)
    else:
        sub=TutorSubscription(user_id=uid,amount_monthly=int(d.get('amount',1500)),started_at=now,ends_at=now+_td(days=30),is_active=True,created_by=u.id,note=d.get('note','Ручное подтверждение'))
        db.add(sub); db.flush()
    db.add(SubscriptionPayment(subscription_id=sub.id,amount=int(d.get('amount',1500)),period=now.strftime('%Y-%m'),paid_at=now,recorded_by=u.id,status=SubscriptionPaymentStatus.confirmed,note=d.get('note')))
    t.subscription_status="active"
    ends_str=sub.ends_at.strftime('%d.%m.%Y') if sub.ends_at else '-'
    db.add(Notification(user_id=t.id,text=f"Платёж подтверждён. Подписка активна до {ends_str}.",notif_type="payment_confirmed",link="/hbm_tutor.html"))
    db.commit(); return {"ok":True}

@app.post("/api/owner/users/{uid}/deactivate-subscription")
def deactivate_subscription(uid:str,u:User=Depends(require_owner),db:Session=Depends(get_db)):
    t=db.query(User).filter(User.id==uid).first()
    if not t: raise HTTPException(404,"Пользователь не найден")
    t.subscription_status="expired"; t.is_active=False
    db.commit(); return {"ok":True}

# ═══ LESSON RECORDS ═══

@app.get("/api/lessons")
def list_lessons(u:User=Depends(get_current_user),db:Session=Depends(get_db),
                 tutor_id:str=None,student_id:str=None,date_from:str=None,date_to:str=None,group_id:str=None):
    from sqlalchemy import and_
    q=db.query(LessonRecord)
    if u.role=="student":
        if not u.student_id: return []
        q=q.filter(LessonRecord.student_id==u.student_id)
    elif u.role=="parent":
        sids=[r.student_id for r in db.execute(parent_student_link.select().where(parent_student_link.c.parent_id==u.id)).fetchall()]
        if not sids: return []
        q=q.filter(LessonRecord.student_id.in_(sids))
        if student_id and student_id in sids: q=q.filter(LessonRecord.student_id==student_id)
    elif u.role in ("tutor","demo_tutor","demo_teamlead"): q=q.filter(LessonRecord.tutor_id==u.id)
    elif u.role=="teamlead":
        tids=_team_tutor_ids(u.id,db)
        if u.is_tutor: tids=tids+[u.id]
        q=q.filter(LessonRecord.tutor_id.in_(tids))
        if tutor_id and tutor_id in tids: q=q.filter(LessonRecord.tutor_id==tutor_id)
    elif u.role=="owner":
        if tutor_id: q=q.filter(LessonRecord.tutor_id==tutor_id)
    else: raise HTTPException(403)
    if student_id: q=q.filter(LessonRecord.student_id==student_id)
    if date_from:
        try:
            from datetime import datetime as _dt
            q=q.filter(LessonRecord.held_at>=_dt.fromisoformat(date_from))
        except: pass
    if date_to:
        try:
            from datetime import datetime as _dt
            q=q.filter(LessonRecord.held_at<=_dt.fromisoformat(date_to))
        except: pass
    if group_id: q=q.filter(LessonRecord.group_id==group_id)
    records=q.order_by(LessonRecord.held_at.desc()).limit(500).all()
    result=[]
    for r in records:
        tutor=db.query(User).filter(User.id==r.tutor_id).first()
        st=db.query(Student).filter(Student.id==r.student_id).first()
        _grp=db.query(Group).filter(Group.id==r.group_id).first() if r.group_id else None
        result.append({"id":r.id,"tutor_id":r.tutor_id,"tutor_name":tutor.name if tutor else None,
                       "student_id":r.student_id,"student_name":st.name if st else None,
                       "held_at":r.held_at.isoformat() if r.held_at else None,
                       "duration_min":r.duration_min,"rate":r.rate,"amount":r.amount,
                       "note":r.note,"slot_id":r.slot_id,
                       "status":r.status or "conducted",
                       "payment_status":r.payment_status or "unpaid",
                       "is_auto":bool(r.is_auto),
                       "group_id":r.group_id,
                       "group_name":_grp.name if _grp else None})
    return result

@app.post("/api/lessons",status_code=201)
def create_lesson(d:dict=Body(...),u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    tutor_id=d.get("tutor_id",u.id)
    if u.role in ("tutor","demo_tutor","demo_teamlead") and tutor_id!=u.id: raise HTTPException(403,"Тьютор может логировать только свои занятия")
    if u.role=="teamlead":
        tids=_team_tutor_ids(u.id,db)
        if tutor_id not in tids: raise HTTPException(403,"Тьютор не в вашей команде")
    tutor=db.query(User).filter(User.id==tutor_id).first()
    if not tutor: raise HTTPException(404,"Тьютор не найден")
    from datetime import datetime as _dt
    held_at_str=d.get("held_at")
    held_at=_dt.fromisoformat(held_at_str) if held_at_str else _dt.now(timezone.utc)
    rate=int(d.get("rate",0)); duration_min=int(d.get("duration_min",60))
    group_id=d.get("group_id")
    if group_id:
        # Групповой урок — создаём запись для каждого активного участника
        grp=db.query(Group).filter(Group.id==group_id).first()
        if not grp: raise HTTPException(404,"Группа не найдена")
        members=db.query(GroupMembership).filter(
            GroupMembership.group_id==group_id,GroupMembership.left_at==None).all()
        if not members: raise HTTPException(400,"В группе нет активных участников")
        ids=[]
        for m in members:
            lr=LessonRecord(id=gen_id(),tutor_id=tutor_id,student_id=m.student_id,
                group_id=group_id,held_at=held_at,duration_min=duration_min,
                rate=rate,amount=rate,note=d.get("note"),
                slot_id=d.get("slot_id"),created_by=u.id)
            db.add(lr); ids.append(lr.id)
            if tutor.subscription_model=="percent" and tutor.subscription_status=="active":
                lr.commission_status="unpaid"; lr.commission_amount=round(rate*(tutor.commission_rate or 5)/100)
        db.commit()
        return {"ids":ids,"count":len(ids),"ok":True}
    # Индивидуальный урок
    student_id=d.get("student_id")
    if not student_id or not db.query(Student).filter(Student.id==student_id).first(): raise HTTPException(404,"Ученик не найден")
    if not rate: rate=1500
    lr=LessonRecord(id=gen_id(),tutor_id=tutor_id,student_id=student_id,held_at=held_at,
                    duration_min=duration_min,rate=rate,amount=rate,
                    note=d.get("note"),slot_id=d.get("slot_id"),created_by=u.id)
    if tutor.subscription_model=="percent" and tutor.subscription_status=="active":
        lr.commission_status="unpaid"; lr.commission_amount=round(rate*(tutor.commission_rate or 5)/100)
    db.add(lr); db.commit(); db.refresh(lr)
    return {"id":lr.id,"ok":True}

@app.delete("/api/lessons/{lid}",status_code=204)
def delete_lesson(lid:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    r=db.query(LessonRecord).filter(LessonRecord.id==lid).first()
    if not r: raise HTTPException(404)
    if u.role=="tutor" and r.tutor_id!=u.id: raise HTTPException(403)
    if u.role=="teamlead":
        tids=_team_tutor_ids(u.id,db)
        if r.tutor_id not in tids: raise HTTPException(403)
    db.delete(r); db.commit()

@app.get("/api/owner/stats")
def owner_stats(u:User=Depends(require_owner),db:Session=Depends(get_db),
                date_from:str=None,date_to:str=None,tutor_id:str=None):
    from datetime import datetime as _dt
    now=_dt.now(timezone.utc)
    df=_dt.fromisoformat(date_from) if date_from else _dt(now.year,now.month,1,tzinfo=timezone.utc)
    dt_=_dt.fromisoformat(date_to) if date_to else now
    q=db.query(LessonRecord).filter(LessonRecord.held_at>=df,LessonRecord.held_at<=dt_)
    if tutor_id: q=q.filter(LessonRecord.tutor_id==tutor_id)
    records=q.all()
    tutor_ids=list(set(r.tutor_id for r in records))
    tutors={t.id:t for t in db.query(User).filter(User.id.in_(tutor_ids)).all()} if tutor_ids else {}
    by_tutor={}
    for r in records:
        t=tutors.get(r.tutor_id); tid=r.tutor_id; nc=t.no_commission if t else False
        if tid not in by_tutor:
            by_tutor[tid]={"id":tid,"name":t.name if t else tid,"lessons":0,"amount":0,"commission":0,"no_commission":nc}
        by_tutor[tid]["lessons"]+=1; by_tutor[tid]["amount"]+=r.amount
        if not nc: by_tutor[tid]["commission"]+=round(r.amount*0.05)
    total_amount=sum(r.amount for r in records)
    total_commission=sum(v["commission"] for v in by_tutor.values())
    by_week={}
    for r in records:
        if r.held_at:
            wk=r.held_at.strftime("%Y-W%W")
            if wk not in by_week: by_week[wk]={"week":wk,"amount":0,"commission":0}
            by_week[wk]["amount"]+=r.amount
            t=tutors.get(r.tutor_id)
            if not(t and t.no_commission): by_week[wk]["commission"]+=round(r.amount*0.05)
    by_group={}
    for r in records:
        if r.group_id:
            if r.group_id not in by_group:
                _grp=db.query(Group).filter(Group.id==r.group_id).first()
                by_group[r.group_id]={"group_id":r.group_id,"group_name":_grp.name if _grp else r.group_id,"lessons":0,"unique_students":set(),"amount":0}
            by_group[r.group_id]["lessons"]+=1
            if r.student_id: by_group[r.group_id]["unique_students"].add(r.student_id)
            by_group[r.group_id]["amount"]+=r.amount
    by_group_list=[{**v,"student_count":len(v.pop("unique_students"))} for v in by_group.values()]
    return {"total_lessons":len(records),"total_amount":total_amount,"total_commission":total_commission,
            "by_tutor":list(by_tutor.values()),"by_week":sorted(by_week.values(),key=lambda x:x["week"]),
            "by_group":by_group_list}

@app.get("/api/teamlead/stats")
def teamlead_stats(u:User=Depends(require_teamlead_or_owner),db:Session=Depends(get_db),
                   date_from:str=None,date_to:str=None,tutor_id:str=None):
    from sqlalchemy import func as sqlfunc
    from datetime import datetime as _dt
    now=_dt.now(timezone.utc)
    df=_dt.fromisoformat(date_from) if date_from else _dt(now.year,now.month,1,tzinfo=timezone.utc)
    dt=_dt.fromisoformat(date_to) if date_to else now
    if u.role=="teamlead":
        tids=_team_tutor_ids(u.id,db)
        if u.is_tutor: tids=tids+[u.id]
    else: # owner viewing a specific team
        tids=[r.id for r in db.query(User.id).filter(User.role=="tutor",User.teamlead_id!=None).all()]
    if tutor_id and tutor_id in tids: tids=[tutor_id]
    if not tids: return {"total_lessons":0,"total_amount":0,"by_tutor":[],"by_week":[],"by_student":[]}
    q=db.query(LessonRecord).filter(LessonRecord.tutor_id.in_(tids),LessonRecord.held_at>=df,LessonRecord.held_at<=dt)
    records=q.all()
    total_lessons=len(records)
    total_amount=sum(r.amount for r in records)
    # By tutor
    tutor_map={}
    for r in records:
        if r.tutor_id not in tutor_map: tutor_map[r.tutor_id]={"count":0,"amount":0}
        tutor_map[r.tutor_id]["count"]+=1; tutor_map[r.tutor_id]["amount"]+=r.amount
    by_tutor=[]
    for tid,v in tutor_map.items():
        t=db.query(User).filter(User.id==tid).first()
        by_tutor.append({"tutor_id":tid,"tutor_name":t.name if t else tid,"lessons_count":v["count"],"amount":v["amount"]})
    # By student
    student_map={}
    for r in records:
        if r.student_id not in student_map: student_map[r.student_id]={"count":0,"amount":0}
        student_map[r.student_id]["count"]+=1; student_map[r.student_id]["amount"]+=r.amount
    by_student=[]
    for sid,v in student_map.items():
        s=db.query(Student).filter(Student.id==sid).first()
        by_student.append({"student_id":sid,"student_name":s.name if s else sid,"lessons_count":v["count"],"amount":v["amount"]})
    # By week (ISO week)
    week_map={}
    for r in records:
        if not r.held_at: continue
        week_key=r.held_at.strftime("%Y-W%V")
        week_start=r.held_at.strftime("%Y-%m-%d")
        if week_key not in week_map: week_map[week_key]={"week_start":week_start,"count":0,"amount":0}
        week_map[week_key]["count"]+=1; week_map[week_key]["amount"]+=r.amount
    by_week=sorted(week_map.values(),key=lambda x:x["week_start"])
    return {"total_lessons":total_lessons,"total_amount":total_amount,
            "by_tutor":by_tutor,"by_week":by_week,"by_student":by_student,
            "date_from":df.isoformat(),"date_to":dt.isoformat()}

# ═══ LESSON & PAYMENT STATUS (Этап 2) ═══

def _can_change_payment_status(actor:User, lesson:LessonRecord, db:Session) -> bool:
    """Иерархия прав: owner > tutor/teamlead > student/parent."""
    if actor.role=="owner": return True
    if actor.role in ("tutor","teamlead"):
        if actor.role=="tutor": return lesson.tutor_id==actor.id
        return lesson.tutor_id in _team_tutor_ids(actor.id,db)
    # student / parent
    if actor.role=="student":
        return actor.student_id==lesson.student_id
    if actor.role=="parent":
        sids=[r.student_id for r in db.execute(parent_student_link.select().where(parent_student_link.c.parent_id==actor.id)).fetchall()]
        return lesson.student_id in sids
    return False

@app.patch("/api/lessons/{lid}/payment-status")
def set_payment_status(lid:str, d:dict=Body(...), u:User=Depends(get_current_user), db:Session=Depends(get_db)):
    lr=db.query(LessonRecord).filter(LessonRecord.id==lid).first()
    if not lr: raise HTTPException(404)
    new_status=d.get("status")
    if new_status not in ("unpaid","paid","disputed"): raise HTTPException(400,"Статус: unpaid / paid / disputed")
    if not _can_change_payment_status(u,lr,db): raise HTTPException(403,"Нет доступа")
    lr.payment_status=new_status
    lr.payment_confirmed_by=u.id
    from datetime import datetime as _dt
    lr.payment_confirmed_at=_dt.now(timezone.utc)
    db.commit()
    return {"ok":True,"payment_status":new_status}

@app.patch("/api/lessons/{lid}/lesson-status")
def set_lesson_status(lid:str, d:dict=Body(...), u:User=Depends(get_current_user), db:Session=Depends(get_db)):
    lr=db.query(LessonRecord).filter(LessonRecord.id==lid).first()
    if not lr: raise HTTPException(404)
    new_status=d.get("status")
    if new_status not in ("conducted","cancelled","rescheduled"): raise HTTPException(400,"Статус: conducted / cancelled / rescheduled")
    if u.role not in ("owner","tutor","teamlead"): raise HTTPException(403)
    if u.role=="tutor" and lr.tutor_id!=u.id: raise HTTPException(403)
    if u.role=="teamlead" and lr.tutor_id not in _team_tutor_ids(u.id,db): raise HTTPException(403)
    lr.status=new_status
    db.commit()
    return {"ok":True,"status":new_status}

@app.get("/api/student-payments")
def list_student_payments(u:User=Depends(get_current_user), db:Session=Depends(get_db),
                          student_id:str=None, date_from:str=None, date_to:str=None):
    from datetime import datetime as _dt
    q=db.query(StudentPayment)
    if u.role=="student":
        if not u.student_id: raise HTTPException(403)
        q=q.filter(StudentPayment.student_id==u.student_id)
    elif u.role=="parent":
        sids=[r.student_id for r in db.execute(parent_student_link.select().where(parent_student_link.c.parent_id==u.id)).fetchall()]
        q=q.filter(StudentPayment.student_id.in_(sids))
    elif u.role=="tutor":
        sids=_tutor_student_ids(u.id,db)
        q=q.filter(StudentPayment.student_id.in_(sids))
        if student_id and student_id in sids: q=q.filter(StudentPayment.student_id==student_id)
    elif u.role=="teamlead":
        sids=_teamlead_student_ids(u.id,db)
        q=q.filter(StudentPayment.student_id.in_(sids))
        if student_id and student_id in sids: q=q.filter(StudentPayment.student_id==student_id)
    elif u.role=="owner":
        if student_id: q=q.filter(StudentPayment.student_id==student_id)
    else: raise HTTPException(403)
    if date_from:
        try: q=q.filter(StudentPayment.paid_at>=_dt.fromisoformat(date_from))
        except: pass
    if date_to:
        try: q=q.filter(StudentPayment.paid_at<=_dt.fromisoformat(date_to))
        except: pass
    return q.order_by(StudentPayment.paid_at.desc()).limit(500).all()

@app.post("/api/student-payments", status_code=201)
def create_student_payment(d:StudentPaymentCreate, u:User=Depends(get_current_user), db:Session=Depends(get_db)):
    if u.role not in ("owner","tutor","teamlead"): raise HTTPException(403)
    if not db.query(Student).filter(Student.id==d.student_id).first(): raise HTTPException(404,"Ученик не найден")
    from datetime import datetime as _dt
    sp=StudentPayment(id=gen_id(),student_id=d.student_id,recorded_by=u.id,
                      amount=d.amount,paid_at=d.paid_at or _dt.now(timezone.utc),note=d.note)
    db.add(sp); db.commit(); db.refresh(sp)
    return {"id":sp.id,"ok":True}

@app.delete("/api/student-payments/{pid}", status_code=204)
def delete_student_payment(pid:str, u:User=Depends(require_tutor_or_owner), db:Session=Depends(get_db)):
    sp=db.query(StudentPayment).filter(StudentPayment.id==pid).first()
    if not sp: raise HTTPException(404)
    if u.role=="tutor":
        sids=_tutor_student_ids(u.id,db)
        if sp.student_id not in sids: raise HTTPException(403)
    db.delete(sp); db.commit()

@app.get("/api/student-balance/{student_id}")
def student_balance(student_id:str, u:User=Depends(get_current_user), db:Session=Depends(get_db)):
    """Баланс ученика: долг = сумма неоплаченных проведённых занятий."""
    # Проверка доступа
    if u.role=="student" and u.student_id!=student_id: raise HTTPException(403)
    if u.role=="parent":
        sids=[r.student_id for r in db.execute(parent_student_link.select().where(parent_student_link.c.parent_id==u.id)).fetchall()]
        if student_id not in sids: raise HTTPException(403)
    st=db.query(Student).filter(Student.id==student_id).first()
    if not st: raise HTTPException(404)
    lessons=db.query(LessonRecord).filter(LessonRecord.student_id==student_id).all()
    debt=sum(l.amount for l in lessons if l.status=="conducted" and l.payment_status=="unpaid")
    paid_total=sum(l.amount for l in lessons if l.status=="conducted" and l.payment_status=="paid")
    return {"student_id":student_id,"debt":debt,"paid_total":paid_total,"lessons_count":len(lessons)}

# ═══ COMMISSION PAYMENTS (Этап 3) ═══

def _commission_accrued(user_id:str, db:Session) -> int:
    """Начислено комиссии = сумма оплаченных проведённых занятий * ставка."""
    u=db.query(User).filter(User.id==user_id).first()
    if not u: return 0
    rate=(u.commission_rate or 5)/100
    lessons=db.query(LessonRecord).filter(
        LessonRecord.tutor_id==user_id,
        LessonRecord.status=="conducted",
        LessonRecord.payment_status=="paid").all()
    return round(sum(l.amount*rate for l in lessons))

def _commission_paid(user_id:str, db:Session) -> int:
    """Оплачено комиссии = сумма confirmed commission_payments."""
    rows=db.query(CommissionPayment).filter(
        CommissionPayment.user_id==user_id,
        CommissionPayment.status=="confirmed").all()
    return sum(r.amount for r in rows)

@app.get("/api/commission-payments")
def list_commission_payments(u:User=Depends(get_current_user), db:Session=Depends(get_db), user_id:str=None):
    if u.role=="owner":
        q=db.query(CommissionPayment)
        if user_id: q=q.filter(CommissionPayment.user_id==user_id)
    elif u.role in ("tutor","teamlead"):
        q=db.query(CommissionPayment).filter(CommissionPayment.user_id==u.id)
    else: raise HTTPException(403)
    return q.order_by(CommissionPayment.paid_at.desc()).limit(200).all()

@app.get("/api/commission-summary")
def commission_summary(u:User=Depends(get_current_user), db:Session=Depends(get_db)):
    """Сводка: начислено / оплачено / долг. Для tutor — своё, для owner — все percent-тьюторы."""
    if u.role in ("tutor","teamlead"):
        if u.subscription_model!="percent": raise HTTPException(400,"Не процентная модель")
        accrued=_commission_accrued(u.id,db); paid=_commission_paid(u.id,db)
        return {"user_id":u.id,"accrued":accrued,"paid":paid,"debt":accrued-paid,"rate":u.commission_rate or 5}
    if u.role=="owner":
        tutors=db.query(User).filter(User.subscription_model=="percent").all()
        result=[]
        for t in tutors:
            accrued=_commission_accrued(t.id,db); paid=_commission_paid(t.id,db)
            result.append({"user_id":t.id,"name":t.name,"role":t.role,"teamlead_id":t.teamlead_id,
                           "accrued":accrued,"paid":paid,"debt":accrued-paid,"rate":t.commission_rate or 5})
        return result
    raise HTTPException(403)

@app.post("/api/commission-payments", status_code=201)
def create_commission_payment(d:CommissionPaymentCreate, u:User=Depends(get_current_user),
                               db:Session=Depends(get_db), for_user:str=None):
    """Тьютор/TL сообщает об оплате комиссии (→ pending). Owner может создать для любого (confirmed)."""
    if u.role not in ("owner","tutor","teamlead"): raise HTTPException(403)
    target_id=for_user if (u.role=="owner" and for_user) else u.id
    if for_user and u.role!="owner": raise HTTPException(403)
    status="confirmed" if u.role=="owner" else "pending"
    from datetime import datetime as _dt
    import json as _json
    covers=_json.dumps(d.covers_lessons) if d.covers_lessons else None
    cp=CommissionPayment(id=gen_id(), user_id=target_id, amount=d.amount,
        paid_at=d.paid_at or _dt.now(timezone.utc), covers_lessons=covers,
        status=status, recorded_by=u.id, note=d.note)
    db.add(cp); db.commit(); db.refresh(cp)
    return {"id":cp.id,"status":status,"ok":True}

@app.patch("/api/commission-payments/{pid}/confirm")
def confirm_commission_payment(pid:str, u:User=Depends(require_owner), db:Session=Depends(get_db)):
    cp=db.query(CommissionPayment).filter(CommissionPayment.id==pid).first()
    if not cp: raise HTTPException(404)
    cp.status="confirmed"; db.commit()
    try: _try_create_recruitment_reward(cp.user_id,"commission",cp.id,cp.amount,db)
    except Exception as _e: print(f"[reward trigger] {_e}")
    return {"ok":True}

@app.delete("/api/commission-payments/{pid}", status_code=204)
def delete_commission_payment(pid:str, u:User=Depends(require_owner), db:Session=Depends(get_db)):
    cp=db.query(CommissionPayment).filter(CommissionPayment.id==pid).first()
    if not cp: raise HTTPException(404)
    db.delete(cp); db.commit()

# ═══ SUBSCRIPTIONS (Этап 1) ═══

@app.get("/api/subscriptions")
def list_subscriptions(u:User=Depends(get_current_user),db:Session=Depends(get_db),user_id:str=None):
    """Owner: все подписки (или конкретного user_id). Tutor/TL: только своя."""
    if u.role=="owner":
        q=db.query(TutorSubscription)
        if user_id: q=q.filter(TutorSubscription.user_id==user_id)
        subs=q.order_by(TutorSubscription.created_at.desc()).all()
    elif u.role in ("tutor","teamlead"):
        subs=db.query(TutorSubscription).filter(TutorSubscription.user_id==u.id).order_by(TutorSubscription.created_at.desc()).all()
    else:
        raise HTTPException(403)
    result=[]
    for s in subs:
        usr=db.query(User).filter(User.id==s.user_id).first()
        payments=[SubscriptionPaymentOut.model_validate(p) for p in sorted(s.payments,key=lambda x:x.paid_at or x.created_at,reverse=True)]
        result.append(TutorSubscriptionOut(
            id=s.id,user_id=s.user_id,amount_monthly=s.amount_monthly,
            started_at=s.started_at,ends_at=s.ends_at,is_active=s.is_active,
            note=s.note,created_by=s.created_by,created_at=s.created_at,
            user_name=usr.name if usr else None,
            user_role=usr.role if usr else None,
            payments=payments
        ))
    return result

@app.get("/api/subscriptions/my")
def my_subscription(u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    """Текущая активная подписка пользователя."""
    if u.role not in ("tutor","teamlead","owner"): raise HTTPException(403)
    sub=db.query(TutorSubscription).filter(TutorSubscription.user_id==u.id,TutorSubscription.is_active==True).first()
    if not sub: return None
    payments=[SubscriptionPaymentOut.model_validate(p) for p in sorted(sub.payments,key=lambda x:x.paid_at or x.created_at,reverse=True)]
    return TutorSubscriptionOut(
        id=sub.id,user_id=sub.user_id,amount_monthly=sub.amount_monthly,
        started_at=sub.started_at,ends_at=sub.ends_at,is_active=sub.is_active,
        note=sub.note,created_by=sub.created_by,created_at=sub.created_at,
        user_name=u.name,user_role=u.role,payments=payments
    )

@app.post("/api/subscriptions",status_code=201)
def create_subscription(d:TutorSubscriptionCreate,u:User=Depends(require_owner),db:Session=Depends(get_db)):
    """Owner создаёт подписку для тьютора/тимлида. Старая деактивируется."""
    target=db.query(User).filter(User.id==d.user_id).first()
    if not target: raise HTTPException(404,"Пользователь не найден")
    if target.role not in ("tutor","teamlead"): raise HTTPException(400,"Подписка только для tutor/teamlead")
    # Деактивировать предыдущую активную подписку
    db.query(TutorSubscription).filter(TutorSubscription.user_id==d.user_id,TutorSubscription.is_active==True).update({"is_active":False})
    from datetime import datetime as _dt
    sub=TutorSubscription(
        id=gen_id(),user_id=d.user_id,amount_monthly=d.amount_monthly,
        started_at=d.started_at or _dt.now(timezone.utc),
        ends_at=d.ends_at,is_active=d.is_active,note=d.note,created_by=u.id
    )
    db.add(sub); db.commit(); db.refresh(sub)
    # Обновить subscription_model на пользователе
    if d.amount_monthly>0 and target.subscription_model is None:
        target.subscription_model="fixed"; db.commit()
    return {"id":sub.id,"ok":True}

@app.patch("/api/subscriptions/{sid}")
def update_subscription(sid:str,d:TutorSubscriptionUpdate,u:User=Depends(require_owner),db:Session=Depends(get_db)):
    """Owner обновляет условия подписки."""
    sub=db.query(TutorSubscription).filter(TutorSubscription.id==sid).first()
    if not sub: raise HTTPException(404)
    if d.amount_monthly is not None: sub.amount_monthly=d.amount_monthly
    if d.started_at is not None: sub.started_at=d.started_at
    if d.ends_at is not None: sub.ends_at=d.ends_at
    if d.is_active is not None: sub.is_active=d.is_active
    if d.note is not None: sub.note=d.note
    db.commit(); return {"ok":True}

@app.delete("/api/subscriptions/{sid}",status_code=204)
def delete_subscription(sid:str,u:User=Depends(require_owner),db:Session=Depends(get_db)):
    sub=db.query(TutorSubscription).filter(TutorSubscription.id==sid).first()
    if not sub: raise HTTPException(404)
    db.delete(sub); db.commit()

@app.post("/api/subscriptions/{sid}/payments",status_code=201)
def report_subscription_payment(sid:str,d:SubscriptionPaymentCreate,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    """Тьютор/TL сообщает об оплате за период (→ pending). Owner сразу создаёт confirmed."""
    sub=db.query(TutorSubscription).filter(TutorSubscription.id==sid).first()
    if not sub: raise HTTPException(404)
    if u.role not in ("owner","tutor","teamlead"): raise HTTPException(403)
    if u.role in ("tutor","teamlead") and sub.user_id!=u.id: raise HTTPException(403)
    # Проверить: не дублировать период
    existing=db.query(SubscriptionPayment).filter(SubscriptionPayment.subscription_id==sid,SubscriptionPayment.period==d.period).first()
    if existing: raise HTTPException(409,f"Платёж за {d.period} уже существует (статус: {existing.status})")
    from datetime import datetime as _dt
    status="confirmed" if u.role=="owner" else "pending"
    pay=SubscriptionPayment(
        id=gen_id(),subscription_id=sid,amount=d.amount,period=d.period,
        paid_at=d.paid_at or _dt.now(timezone.utc),
        recorded_by=u.id,status=status,note=d.note
    )
    db.add(pay); db.commit(); db.refresh(pay)
    return {"id":pay.id,"status":status,"ok":True}

@app.patch("/api/subscriptions/{sid}/payments/{pid}/confirm")
def confirm_subscription_payment(sid:str,pid:str,u:User=Depends(require_owner),db:Session=Depends(get_db)):
    """Owner подтверждает платёж тьютора."""
    pay=db.query(SubscriptionPayment).filter(SubscriptionPayment.id==pid,SubscriptionPayment.subscription_id==sid).first()
    if not pay: raise HTTPException(404)
    pay.status="confirmed"; db.commit()
    try:
        sub=db.query(TutorSubscription).filter(TutorSubscription.id==sid).first()
        if sub: _try_create_recruitment_reward(sub.user_id,"subscription",pay.id,pay.amount,db)
    except Exception as _e: print(f"[reward trigger] {_e}")
    return {"ok":True}

@app.delete("/api/subscriptions/{sid}/payments/{pid}",status_code=204)
def delete_subscription_payment(sid:str,pid:str,u:User=Depends(require_owner),db:Session=Depends(get_db)):
    pay=db.query(SubscriptionPayment).filter(SubscriptionPayment.id==pid,SubscriptionPayment.subscription_id==sid).first()
    if not pay: raise HTTPException(404)
    db.delete(pay); db.commit()

# ═══ STATIC ═══
app.mount("/uploads",StaticFiles(directory=UPLOAD_DIR),name="uploads")
app.mount("/cm",StaticFiles(directory="static/cm"),name="cm")
SD="static"
if os.path.isdir(SD):
    from fastapi.responses import FileResponse,HTMLResponse
    _NC={"Cache-Control":"no-cache, no-store, must-revalidate","Pragma":"no-cache","Expires":"0"}
    @app.get("/api.js")
    async def sjs(): return FileResponse(os.path.join(SD,"api.js"),media_type="application/javascript",headers=_NC)
    @app.get("/call.js")
    async def scalljs(): return FileResponse(os.path.join(SD,"call.js"),media_type="application/javascript",headers=_NC)
    @app.get("/{p}.html")
    async def shtml(p:str):
        fp=os.path.join(SD,f"{p}.html")
        if os.path.isfile(fp): return FileResponse(fp,media_type="text/html",headers=_NC)
        raise HTTPException(404)
    @app.get("/")
    async def idx():
        fp=os.path.join(SD,"index.html")
        if os.path.isfile(fp): return FileResponse(fp)
        return HTMLResponse("HBM")


class LeadCreate(BaseModel):
    name: str
    phone: str
    email: Optional[str] = None
    subject: Optional[str] = None
    source: Optional[str] = None
    message: Optional[str] = None

@app.post("/api/lead", status_code=201)
def submit_lead(data: LeadCreate, db: Session = Depends(get_db)):
    from models import Lead
    lead = Lead(
        id=str(uuid.uuid4()),
        name=data.name.strip(),
        phone=data.phone.strip(),
        email=data.email,
        subject=data.subject,
        source=data.source,
        note=data.message,
    )
    db.add(lead); db.commit()
    return {"ok": True}

@app.get("/api/leads")
def get_leads(u: User = Depends(require_owner), db: Session = Depends(get_db)):
    from models import Lead
    leads = db.query(Lead).order_by(Lead.created_at.desc()).all()
    return [
        {"id": l.id, "name": l.name, "phone": l.phone, "email": l.email,
         "subject": l.subject, "source": l.source, "status": l.status,
         "note": l.note, "created_at": l.created_at.isoformat() if l.created_at else None}
        for l in leads
    ]

@app.patch("/api/leads/{lead_id}")
def update_lead(lead_id: str, data: dict, u: User = Depends(require_owner), db: Session = Depends(get_db)):
    from models import Lead
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(404, "Заявка не найдена")
    for k, v in data.items():
        if k in ("status", "note"):
            setattr(lead, k, v)
    db.commit()
    return {"ok": True}

@app.delete("/api/leads/{lead_id}")
def delete_lead(lead_id: str, u: User = Depends(require_owner), db: Session = Depends(get_db)):
    from models import Lead
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(404, "Заявка не найдена")
    db.delete(lead)
    db.commit()
    return {"ok": True}




# ════════════════════════════════════════════════════════════════════════════
# TZ: Новая функциональность
# ════════════════════════════════════════════════════════════════════════════

# ── Auth helpers ─────────────────────────────────────────────────────────────

def require_recruiter_or_owner(u: User = Depends(get_current_user)):
    if u.role not in ("owner", "recruiter"):
        raise HTTPException(403, "Только для рекрутёра или владельца")
    return u


# ── Демо: генерация тестовых данных ──────────────────────────────────────────

def _generate_demo_data(user_id: str, db: Session):
    import random, datetime as _dt
    from sqlalchemy import text as _t
    NAMES = ["Алексей Петров", "Мария Сидорова", "Иван Козлов", "Анна Новикова", "Дмитрий Волков"]
    LEVELS = ["school", "school", "school", "university", "additional"]
    GRADES = ["9", "10", "11", None, None]
    GOALS  = ["ege", "oge", "ege", "improve_grades", "deepening"]
    student_ids = []
    for i, nm in enumerate(NAMES):
        sid = secrets.token_hex(6)
        db.execute(_t("INSERT INTO students(id,name,level,grade,goal,base_rate,format,is_demo,created_by) VALUES(:id,:nm,:lv,:gr,:gl,:rt,'online',TRUE,:by)"),
            {"id":sid,"nm":nm,"lv":LEVELS[i],"gr":GRADES[i],"gl":GOALS[i],"rt":random.choice([1200,1500,1800,2000]),"by":user_id})
        db.execute(_t("INSERT INTO tutor_student_link(tutor_id,student_id) VALUES(:t,:s) ON CONFLICT DO NOTHING"), {"t":user_id,"s":sid})
        student_ids.append(sid)
    used = set()
    for sid in student_ids:
        day = random.randint(0,4); slot = random.choice([16,20,24,28,32,36]); key=(day,slot)
        if key not in used:
            used.add(key); slid = secrets.token_hex(6)
            db.execute(_t("INSERT INTO schedule_slots(id,tutor_id,student_id,day_of_week,slot_index,duration,is_demo) VALUES(:id,:t,:s,:d,:sl,2,TRUE)"),
                {"id":slid,"t":user_id,"s":sid,"d":day,"sl":slot})
    now = _dt.datetime.now(_dt.timezone.utc)
    for _ in range(random.randint(20, 28)):
        sid = random.choice(student_ids); days_back = random.randint(1,30)
        held = now - _dt.timedelta(days=days_back)
        stat = random.choice(["conducted","conducted","conducted","cancelled"])
        pstat = random.choice(["paid","paid","unpaid","unpaid"]) if stat=="conducted" else "unpaid"
        rate = random.choice([1200,1500,1800,2000]); lid = secrets.token_hex(6)
        db.execute(_t("INSERT INTO lesson_records(id,tutor_id,student_id,held_at,duration_min,rate,amount,status,payment_status,is_auto,is_demo) VALUES(:id,:t,:s,:h,60,:r,:r,:st,:ps,FALSE,TRUE)"),
            {"id":lid,"t":user_id,"s":sid,"h":held,"r":rate,"st":stat,"ps":pstat})
    db.commit()


# ── Демо: фоновый воркер очистки ─────────────────────────────────────────────

async def _demo_cleanup_loop():
    await asyncio.sleep(60)
    while True:
        try:
            _cleanup_expired_demos()
        except Exception as e:
            print(f"[demo_cleanup] {e}")
        await asyncio.sleep(3600)

def _cleanup_expired_demos():
    from sqlalchemy import text as _t
    from datetime import timezone as _tz
    db = SessionLocal()
    try:
        now = datetime.now(_tz.utc)
        expired = db.execute(_t("SELECT id FROM users WHERE role IN ('demo_tutor','demo_teamlead') AND demo_expires_at IS NOT NULL AND demo_expires_at < :now"), {"now": now}).fetchall()
        for (uid,) in expired:
            db.execute(_t("DELETE FROM lesson_records WHERE tutor_id=:u AND is_demo=TRUE"), {"u":uid})
            db.execute(_t("DELETE FROM schedule_slots WHERE tutor_id=:u AND is_demo=TRUE"), {"u":uid})
            db.execute(_t("DELETE FROM course_instances WHERE tutor_id=:u AND is_demo=TRUE"), {"u":uid})
            db.execute(_t("DELETE FROM tutor_student_link WHERE tutor_id=:u"), {"u":uid})
            db.execute(_t("DELETE FROM students WHERE created_by=:u AND is_demo=TRUE"), {"u":uid})
            db.execute(_t("DELETE FROM users WHERE id=:u"), {"u":uid})
        db.commit()
        if expired: print(f"[demo_cleanup] removed {len(expired)} expired demo accounts")
    finally:
        db.close()


# ── Антифрод трекинг WS-сессий ───────────────────────────────────────────────

_board_sessions: dict = {}  # stid -> {tutor_id: session_start}

def _check_board_anomaly(stid: str, tutor_id: str, db: Session):
    from sqlalchemy import text as _t
    from datetime import timezone as _tz
    import datetime as _dt
    now = datetime.now(_tz.utc)
    msk_now = now.replace(tzinfo=None) + _dt.timedelta(hours=3)
    day_of_week = msk_now.weekday()
    current_slot = (msk_now.hour * 60 + msk_now.minute) // 30
    slot = db.execute(_t("SELECT id FROM schedule_slots WHERE tutor_id=:t AND student_id=:s AND day_of_week=:d AND slot_index<=:cs AND (slot_index+duration)>:cs LIMIT 1"),
        {"t":tutor_id,"s":stid,"d":day_of_week,"cs":current_slot}).fetchone()
    if not slot:
        sess = _board_sessions.get(stid, {}); start = sess.get(tutor_id)
        dur = int((now - start).total_seconds() / 60) if start else 5
        fid = secrets.token_hex(6)
        db.execute(_t("INSERT INTO board_anomaly_flags(id,tutor_id,student_id,session_start,session_duration_min) VALUES(:id,:t,:s,:st,:dur)"),
            {"id":fid,"t":tutor_id,"s":stid,"st":start or now,"dur":dur})
        db.commit()


# ════════════════════════════════════════════════════════════════════════════
# API: Активация пользователей
# ════════════════════════════════════════════════════════════════════════════

@app.get("/api/users/pending-activation")
def list_pending_activation(u: User = Depends(require_owner), db: Session = Depends(get_db)):
    users = db.query(User).filter(User.is_active == False).order_by(User.created_at.desc()).all()
    return [_user_out(usr, db) for usr in users]

@app.patch("/api/users/{uid}/activate")
def activate_user(uid: str, u: User = Depends(require_owner), db: Session = Depends(get_db)):
    target = db.query(User).filter(User.id == uid).first()
    if not target: raise HTTPException(404, "Пользователь не найден")
    target.is_active = True
    db.commit()
    return {"ok": True}


# ════════════════════════════════════════════════════════════════════════════
# API: Рекрутёр
# ════════════════════════════════════════════════════════════════════════════

@app.get("/api/recruiter/recruited")
def get_recruited(u: User = Depends(require_recruiter_or_owner), db: Session = Depends(get_db)):
    from sqlalchemy import text as _t
    if u.role == "owner":
        recruits = db.query(User).filter(User.recruited_by != None).all()
    else:
        recruits = db.query(User).filter(User.recruited_by == u.id).all()
    result = []
    for r in recruits:
        sp = db.execute(_t("SELECT sp.id, sp.amount, sp.status FROM subscription_payments sp JOIN tutor_subscriptions ts ON sp.subscription_id=ts.id WHERE ts.user_id=:uid ORDER BY sp.created_at ASC LIMIT 1"), {"uid":r.id}).fetchone()
        cp = db.execute(_t("SELECT id, amount, status FROM commission_payments WHERE user_id=:uid ORDER BY created_at ASC LIMIT 1"), {"uid":r.id}).fetchone()
        first_pay = sp or cp
        rw = db.execute(_t("SELECT status FROM recruitment_rewards WHERE recruited_user_id=:uid LIMIT 1"), {"uid":r.id}).fetchone()
        result.append({"id":r.id,"login":r.login,"name":r.name,"role":r.role,
            "is_active": getattr(r,"is_active",True),
            "recruited_by": getattr(r,"recruited_by",None),
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "first_payment_status": first_pay[2] if first_pay else None,
            "first_payment_amount": first_pay[1] if first_pay else None,
            "reward_status": rw[0] if rw else None})
    return result


@app.get("/api/recruiter/rewards")
def get_rewards(u: User = Depends(require_recruiter_or_owner), db: Session = Depends(get_db)):
    from sqlalchemy import text as _t
    if u.role == "owner":
        rows = db.execute(_t("SELECT rr.*, ru.name as ru_name, rc.name as rc_name FROM recruitment_rewards rr JOIN users ru ON ru.id=rr.recruited_user_id JOIN users rc ON rc.id=rr.recruiter_id ORDER BY rr.created_at DESC")).fetchall()
    else:
        rows = db.execute(_t("SELECT rr.*, ru.name as ru_name, rc.name as rc_name FROM recruitment_rewards rr JOIN users ru ON ru.id=rr.recruited_user_id JOIN users rc ON rc.id=rr.recruiter_id WHERE rr.recruiter_id=:uid ORDER BY rr.created_at DESC"), {"uid":u.id}).fetchall()
    return [dict(r._mapping) for r in rows]


@app.patch("/api/recruiter/rewards/{rid}/confirm")
def confirm_reward(rid: str, u: User = Depends(require_owner), db: Session = Depends(get_db)):
    from sqlalchemy import text as _t
    from datetime import timezone as _tz
    rw = db.execute(_t("SELECT id, status FROM recruitment_rewards WHERE id=:id"), {"id":rid}).fetchone()
    if not rw: raise HTTPException(404, "Не найдено")
    if rw[1] == "confirmed": raise HTTPException(400, "Уже подтверждено")
    db.execute(_t("UPDATE recruitment_rewards SET status='confirmed', confirmed_by=:by, confirmed_at=:at WHERE id=:id"),
        {"by":u.id,"at":datetime.now(_tz.utc),"id":rid})
    db.commit()
    return {"ok": True}


def _try_create_recruitment_reward(paid_user_id: str, payment_type: str, payment_id: str, amount: int, db: Session):
    from sqlalchemy import text as _t
    user = db.query(User).filter(User.id == paid_user_id).first()
    if not user or not getattr(user, "recruited_by", None): return
    recruiter_id = user.recruited_by
    existing_sub = db.execute(_t("SELECT sp.id FROM subscription_payments sp JOIN tutor_subscriptions ts ON sp.subscription_id=ts.id WHERE ts.user_id=:uid AND sp.status='confirmed' AND sp.id!=:pid"), {"uid":paid_user_id,"pid":payment_id}).fetchone()
    existing_com = db.execute(_t("SELECT id FROM commission_payments WHERE user_id=:uid AND status='confirmed' AND id!=:pid"), {"uid":paid_user_id,"pid":payment_id}).fetchone()
    if existing_sub or existing_com: return
    existing_rw = db.execute(_t("SELECT id FROM recruitment_rewards WHERE recruited_user_id=:uid"), {"uid":paid_user_id}).fetchone()
    if existing_rw: return
    rw_id = secrets.token_hex(6)
    db.execute(_t("INSERT INTO recruitment_rewards(id,recruiter_id,recruited_user_id,source_payment_type,source_payment_id,amount) VALUES(:id,:rc,:ru,:pt,:pi,:amt)"),
        {"id":rw_id,"rc":recruiter_id,"ru":paid_user_id,"pt":payment_type,"pi":payment_id,"amt":amount})
    db.commit()


# ════════════════════════════════════════════════════════════════════════════
# API: Демо-аккаунты
# ════════════════════════════════════════════════════════════════════════════

@app.post("/api/demo-accounts")
def create_demo_account(d: DemoAccountCreate, u: User = Depends(require_recruiter_or_owner), db: Session = Depends(get_db)):
    if d.role not in ("demo_tutor", "demo_teamlead"):
        raise HTTPException(400, "Роль должна быть demo_tutor или demo_teamlead")
    import random, string, datetime as _dt
    from datetime import timezone as _tz
    uid = secrets.token_hex(6)
    login = f"demo_{uid}"
    password = "Demo" + "".join(random.choices(string.ascii_letters + string.digits, k=8))
    expires_at = datetime.now(_tz.utc) + _dt.timedelta(hours=48)
    new_user = User(id=uid, login=login, name=d.name, password_hash=hash_password(password),
        role=d.role, must_change_password=False, is_active=True, is_demo=True,
        demo_expires_at=expires_at, recruited_by=u.id if u.role == "recruiter" else None)
    db.add(new_user); db.commit()
    if d.mode == "prefilled":
        try: _generate_demo_data(uid, db)
        except Exception as e: print(f"[demo prefill] {e}")
    return {"id":uid,"login":login,"password":password,"name":d.name,"role":d.role,"expires_at":expires_at.isoformat()}


@app.get("/api/demo-accounts")
def list_demo_accounts(u: User = Depends(require_recruiter_or_owner), db: Session = Depends(get_db)):
    from datetime import timezone as _tz
    q = db.query(User).filter(User.role.in_(["demo_tutor","demo_teamlead"]))
    if u.role == "recruiter": q = q.filter(User.recruited_by == u.id)
    demos = q.order_by(User.created_at.desc()).all()
    now = datetime.now(_tz.utc)
    return [{"id":d.id,"login":d.login,"name":d.name,"role":d.role,
        "expires_at":d.demo_expires_at.isoformat() if d.demo_expires_at else None,
        "is_expired":(d.demo_expires_at < now) if d.demo_expires_at else False,
        "created_by":getattr(d,"recruited_by",None)} for d in demos]


@app.delete("/api/demo-accounts/{uid}")
def delete_demo_account(uid: str, u: User = Depends(require_recruiter_or_owner), db: Session = Depends(get_db)):
    from sqlalchemy import text as _t
    target = db.query(User).filter(User.id == uid).first()
    if not target: raise HTTPException(404, "Не найден")
    if target.role not in ("demo_tutor","demo_teamlead"): raise HTTPException(400, "Не демо-аккаунт")
    if u.role == "recruiter" and getattr(target,"recruited_by",None) != u.id: raise HTTPException(403, "Нет доступа")
    db.execute(_t("DELETE FROM lesson_records WHERE tutor_id=:u AND is_demo=TRUE"), {"u":uid})
    db.execute(_t("DELETE FROM schedule_slots WHERE tutor_id=:u AND is_demo=TRUE"), {"u":uid})
    db.execute(_t("DELETE FROM tutor_student_link WHERE tutor_id=:u"), {"u":uid})
    db.execute(_t("DELETE FROM students WHERE created_by=:u AND is_demo=TRUE"), {"u":uid})
    db.delete(target); db.commit()
    return {"ok": True}


# ════════════════════════════════════════════════════════════════════════════
# API: Антифрод
# ════════════════════════════════════════════════════════════════════════════

@app.get("/api/anomalies")
def list_anomalies(status: str = None, tutor_id: str = None, u: User = Depends(require_owner), db: Session = Depends(get_db)):
    from sqlalchemy import text as _t
    filters = "WHERE 1=1"; params = {}
    if status: filters += " AND baf.status=:status"; params["status"] = status
    if tutor_id: filters += " AND baf.tutor_id=:tutor_id"; params["tutor_id"] = tutor_id
    rows = db.execute(_t(f"SELECT baf.*, u.name as tutor_name, s.name as student_name FROM board_anomaly_flags baf LEFT JOIN users u ON u.id=baf.tutor_id LEFT JOIN students s ON s.id=baf.student_id {filters} ORDER BY baf.created_at DESC LIMIT 200"), params).fetchall()
    return [dict(r._mapping) for r in rows]


@app.patch("/api/anomalies/{fid}/dismiss")
def dismiss_anomaly(fid: str, note: str = Body(None, embed=True), u: User = Depends(require_owner), db: Session = Depends(get_db)):
    from sqlalchemy import text as _t
    from datetime import timezone as _tz
    db.execute(_t("UPDATE board_anomaly_flags SET status='dismissed', dismissed_by=:by, dismissed_at=:at, note=:note WHERE id=:id"),
        {"by":u.id,"at":datetime.now(_tz.utc),"note":note,"id":fid})
    db.commit(); return {"ok": True}


@app.patch("/api/anomalies/{fid}/resolve")
def resolve_anomaly(fid: str, note: str = Body(None, embed=True), u: User = Depends(require_owner), db: Session = Depends(get_db)):
    from sqlalchemy import text as _t
    from datetime import timezone as _tz
    db.execute(_t("UPDATE board_anomaly_flags SET status='resolved', dismissed_by=:by, dismissed_at=:at, note=:note WHERE id=:id"),
        {"by":u.id,"at":datetime.now(_tz.utc),"note":note,"id":fid})
    db.commit(); return {"ok": True}


# ════════════════════════════════════════════════════════════════════════════
# API: Атрибуция — студенты с цепочкой владения
# ════════════════════════════════════════════════════════════════════════════

@app.get("/api/students/attributed")
def students_attributed(tutor_id: str = None, teamlead_id: str = None,
                        u: User = Depends(require_tutor_or_owner), db: Session = Depends(get_db)):
    from sqlalchemy import text as _t
    where = "WHERE s.is_demo=FALSE"; params: dict = {}
    if u.role == "owner":
        if tutor_id: where += " AND tsl.tutor_id=:tutor_id"; params["tutor_id"] = tutor_id
        if teamlead_id: where += " AND u_tutor.teamlead_id=:teamlead_id"; params["teamlead_id"] = teamlead_id
    elif u.role == "teamlead":
        tids = _team_tutor_ids(u.id, db)
        if not tids: return []
        where += " AND tsl.tutor_id = ANY(:tids)"; params["tids"] = tids
    else:
        where += " AND tsl.tutor_id=:uid"; params["uid"] = u.id
    rows = db.execute(_t(f"""
        SELECT DISTINCT ON (s.id)
            s.id, s.name, s.level, s.grade, s.goal, s.base_rate, s.format,
            s.rewards_enabled, s.payer_model, s.subject_id, s.created_by, s.created_at, s.is_demo,
            tsl.tutor_id, u_tutor.name as tutor_name, u_tutor.teamlead_id, u_tl.name as teamlead_name
        FROM students s
        LEFT JOIN tutor_student_link tsl ON tsl.student_id=s.id
        LEFT JOIN users u_tutor ON u_tutor.id=tsl.tutor_id
        LEFT JOIN users u_tl ON u_tl.id=u_tutor.teamlead_id
        {where}
        ORDER BY s.id, s.created_at DESC
    """), params).fetchall()
    result = []
    for r in rows:
        m = dict(r._mapping)
        result.append({**{k: m[k] for k in ("id","name","level","grade","goal","base_rate","format","rewards_enabled","payer_model","subject_id","created_by","is_demo")},
            "created_at": m["created_at"].isoformat() if m["created_at"] else None,
            "tutor": {"id":m["tutor_id"],"name":m["tutor_name"]} if m["tutor_id"] else None,
            "teamlead": {"id":m["teamlead_id"],"name":m["teamlead_name"]} if m["teamlead_id"] else None})
    return result


# ══════════════════════════════════════════════════════════════════════════════
# LESSON GROUPS API
# ══════════════════════════════════════════════════════════════════════════════

def _check_group_access(g, u, db):
    if u.role in ("owner",): return
    if u.role == "teamlead":
        tids = {u.id} | {t.id for t in db.query(User).filter(User.teamlead_id == u.id).all()}
        if g.tutor_id not in tids: raise HTTPException(403, "Нет доступа к группе")
        return
    if g.tutor_id != u.id: raise HTTPException(403, "Нет доступа к группе")


def _group_member_names(members, db):
    sids = [m.student_id for m in members]
    if not sids: return {}
    return {s.id: s.name for s in db.query(Student).filter(Student.id.in_(sids)).all()}


@app.get("/api/lesson-groups")
def list_lesson_groups(u: User = Depends(require_tutor_or_owner), db: Session = Depends(get_db)):
    q = db.query(Group)
    if u.role == "tutor":
        q = q.filter(Group.tutor_id == u.id)
    elif u.role == "teamlead":
        tids = [u.id] + [t.id for t in db.query(User).filter(User.teamlead_id == u.id).all()]
        q = q.filter(Group.tutor_id.in_(tids))
    groups = q.order_by(Group.created_at.desc()).all()
    result = []
    for g in groups:
        cnt = db.query(GroupMembership).filter(
            GroupMembership.group_id == g.id, GroupMembership.left_at == None).count()
        result.append({
            "id": g.id, "name": g.name, "tutor_id": g.tutor_id,
            "subject_id": g.subject_id, "color": g.color, "note": g.note,
            "max_students": g.max_students, "is_demo": g.is_demo,
            "created_at": g.created_at.isoformat() if g.created_at else None,
            "member_count": cnt,
        })
    return result


@app.post("/api/lesson-groups")
def create_lesson_group(d: LessonGroupCreate, u: User = Depends(require_tutor_or_owner), db: Session = Depends(get_db)):
    g = Group(id=secrets.token_hex(6), name=d.name, tutor_id=u.id,
              subject_id=d.subject_id, color=d.color, note=d.note,
              max_students=d.max_students, is_demo=getattr(u, "is_demo", False))
    db.add(g); db.flush()
    # Автоматически создаём чат-группу для беседы и групповых звонков
    cg = ChatGroup(id=secrets.token_hex(6), name=d.name, created_by=u.id, tutor_id=u.id)
    db.add(cg); db.flush()
    g.chat_group_id = cg.id
    db.execute(_sa_text("INSERT INTO chat_group_members(group_id,user_id) VALUES(:g,:u) ON CONFLICT DO NOTHING").bindparams(g=cg.id, u=u.id))
    db.commit(); db.refresh(g)
    return {"id": g.id, "name": g.name, "chat_group_id": g.chat_group_id, "ok": True}


@app.get("/api/lesson-groups/{gid}")
def get_lesson_group(gid: str, u: User = Depends(require_tutor_or_owner), db: Session = Depends(get_db)):
    g = db.query(Group).filter(Group.id == gid).first()
    if not g: raise HTTPException(404, "Группа не найдена")
    _check_group_access(g, u, db)
    members = db.query(GroupMembership).filter(GroupMembership.group_id == gid).all()
    names = _group_member_names(members, db)
    links = db.query(GroupCourseLink).filter(GroupCourseLink.group_id == gid).all()
    return {
        "id": g.id, "name": g.name, "tutor_id": g.tutor_id,
        "subject_id": g.subject_id, "color": g.color, "note": g.note,
        "max_students": g.max_students, "is_demo": g.is_demo,
        "chat_group_id": g.chat_group_id,
        "created_at": g.created_at.isoformat() if g.created_at else None,
        "members": [{"id": m.id, "student_id": m.student_id,
                     "name": names.get(m.student_id, "?"),
                     "joined_at": m.joined_at.isoformat(),
                     "left_at": m.left_at.isoformat() if m.left_at else None} for m in members],
        "courses": [{"id": l.id, "instance_id": l.instance_id, "rate": l.rate,
                     "title": l.instance.title if l.instance else None} for l in links],
    }


@app.patch("/api/lesson-groups/{gid}")
def update_lesson_group(gid: str, d: LessonGroupUpdate, u: User = Depends(require_tutor_or_owner), db: Session = Depends(get_db)):
    g = db.query(Group).filter(Group.id == gid).first()
    if not g: raise HTTPException(404)
    _check_group_access(g, u, db)
    if d.name is not None: g.name = d.name
    if d.subject_id is not None: g.subject_id = d.subject_id
    if d.color is not None: g.color = d.color
    if d.note is not None: g.note = d.note
    if d.max_students is not None: g.max_students = d.max_students
    db.commit()
    return {"ok": True}


@app.delete("/api/lesson-groups/{gid}")
def delete_lesson_group(gid: str, u: User = Depends(require_tutor_or_owner), db: Session = Depends(get_db)):
    if u.role != "owner": raise HTTPException(403, "Только владелец может удалять группы")
    g = db.query(Group).filter(Group.id == gid).first()
    if not g: raise HTTPException(404)
    db.delete(g); db.commit()
    return {"ok": True}


@app.get("/api/lesson-groups/{gid}/members")
def list_group_members(gid: str, u: User = Depends(require_tutor_or_owner), db: Session = Depends(get_db)):
    g = db.query(Group).filter(Group.id == gid).first()
    if not g: raise HTTPException(404)
    _check_group_access(g, u, db)
    members = db.query(GroupMembership).filter(GroupMembership.group_id == gid).all()
    names = _group_member_names(members, db)
    return [{"id": m.id, "student_id": m.student_id, "name": names.get(m.student_id, "?"),
             "joined_at": m.joined_at.isoformat(),
             "left_at": m.left_at.isoformat() if m.left_at else None} for m in members]


@app.post("/api/lesson-groups/{gid}/members")
def add_group_member(gid: str, d: LessonGroupMemberAdd, u: User = Depends(require_tutor_or_owner), db: Session = Depends(get_db)):
    g = db.query(Group).filter(Group.id == gid).first()
    if not g: raise HTTPException(404)
    _check_group_access(g, u, db)
    st = db.query(Student).filter(Student.id == d.student_id).first()
    if not st: raise HTTPException(404, "Ученик не найден")
    existing = db.query(GroupMembership).filter(
        GroupMembership.group_id == gid,
        GroupMembership.student_id == d.student_id,
        GroupMembership.left_at == None).first()
    if existing: raise HTTPException(409, "Ученик уже активный участник группы")
    m = GroupMembership(id=secrets.token_hex(6), group_id=gid, student_id=d.student_id)
    db.add(m); db.flush()
    # Добавляем пользователя ученика в чат-группу
    if g.chat_group_id:
        _su = db.query(User).filter(User.student_id == d.student_id).first()
        if _su:
            db.execute(_sa_text("INSERT INTO chat_group_members(group_id,user_id) VALUES(:g,:u) ON CONFLICT DO NOTHING").bindparams(g=g.chat_group_id, u=_su.id))
    links = db.query(GroupCourseLink).filter(GroupCourseLink.group_id == gid).all()
    for link in links:
        en = db.query(Enrollment).filter(
            Enrollment.student_id == d.student_id,
            Enrollment.instance_id == link.instance_id).first()
        if not en:
            db.add(Enrollment(id=secrets.token_hex(6), student_id=d.student_id, instance_id=link.instance_id))
    db.commit()
    return {"id": m.id, "ok": True}


@app.delete("/api/lesson-groups/{gid}/members/{mid}")
def remove_group_member(gid: str, mid: str, u: User = Depends(require_tutor_or_owner), db: Session = Depends(get_db)):
    g = db.query(Group).filter(Group.id == gid).first()
    if not g: raise HTTPException(404)
    _check_group_access(g, u, db)
    m = db.query(GroupMembership).filter(
        GroupMembership.id == mid, GroupMembership.group_id == gid).first()
    if not m: raise HTTPException(404)
    from datetime import timezone as _tz
    m.left_at = datetime.now(_tz.utc)
    # Убираем пользователя ученика из чат-группы
    if g.chat_group_id:
        _su = db.query(User).filter(User.student_id == m.student_id).first()
        if _su:
            db.execute(_sa_text("DELETE FROM chat_group_members WHERE group_id=:g AND user_id=:u").bindparams(g=g.chat_group_id, u=_su.id))
    db.commit()
    return {"ok": True}


@app.post("/api/lesson-groups/{gid}/courses")
def add_group_course(gid: str, d: LessonGroupCourseLinkCreate, u: User = Depends(require_tutor_or_owner), db: Session = Depends(get_db)):
    g = db.query(Group).filter(Group.id == gid).first()
    if not g: raise HTTPException(404)
    _check_group_access(g, u, db)
    existing = db.query(GroupCourseLink).filter(
        GroupCourseLink.group_id == gid,
        GroupCourseLink.instance_id == d.instance_id).first()
    if existing: raise HTTPException(409, "Программа уже привязана к этой группе")
    link = GroupCourseLink(id=secrets.token_hex(6), group_id=gid,
                           instance_id=d.instance_id, rate=d.rate)
    db.add(link); db.flush()
    members = db.query(GroupMembership).filter(
        GroupMembership.group_id == gid, GroupMembership.left_at == None).all()
    for m in members:
        en = db.query(Enrollment).filter(
            Enrollment.student_id == m.student_id,
            Enrollment.instance_id == d.instance_id).first()
        if not en:
            db.add(Enrollment(id=secrets.token_hex(6), student_id=m.student_id, instance_id=d.instance_id))
    db.commit()
    return {"id": link.id, "ok": True}


@app.patch("/api/lesson-groups/{gid}/courses/{link_id}")
def update_group_course(gid: str, link_id: str, d: LessonGroupCourseLinkUpdate, u: User = Depends(require_tutor_or_owner), db: Session = Depends(get_db)):
    g = db.query(Group).filter(Group.id == gid).first()
    if not g: raise HTTPException(404)
    _check_group_access(g, u, db)
    link = db.query(GroupCourseLink).filter(
        GroupCourseLink.id == link_id, GroupCourseLink.group_id == gid).first()
    if not link: raise HTTPException(404)
    link.rate = d.rate
    db.commit()
    return {"ok": True}


@app.delete("/api/lesson-groups/{gid}/courses/{link_id}")
def remove_group_course(gid: str, link_id: str, u: User = Depends(require_tutor_or_owner), db: Session = Depends(get_db)):
    g = db.query(Group).filter(Group.id == gid).first()
    if not g: raise HTTPException(404)
    _check_group_access(g, u, db)
    link = db.query(GroupCourseLink).filter(
        GroupCourseLink.id == link_id, GroupCourseLink.group_id == gid).first()
    if not link: raise HTTPException(404)
    db.delete(link); db.commit()
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════
# Планы занятий (Study Plans)
# ══════════════════════════════════════════════════════════════════════

@app.get("/api/study-plans")
def list_study_plans(
    student_id: Optional[str] = None,
    group_id: Optional[str] = None,
    u: User = Depends(require_tutor_or_owner),
    db: Session = Depends(get_db)
):
    q = db.query(StudyPlan)
    if u.role not in ("owner",):
        q = q.filter(StudyPlan.tutor_id == u.id)
    if student_id:
        q = q.filter(StudyPlan.student_id == student_id)
    if group_id:
        q = q.filter(StudyPlan.group_id == group_id)
    plans = q.order_by(StudyPlan.created_at.desc()).all()
    result = []
    for p in plans:
        result.append({
            "id": p.id,
            "tutor_id": p.tutor_id,
            "tutor_name": p.tutor.name if p.tutor else None,
            "student_id": p.student_id,
            "student_name": p.student.name if p.student else None,
            "group_id": p.group_id,
            "group_name": p.group.name if p.group else None,
            "subject_id": p.subject_id,
            "subject_name": p.subject.name if p.subject else None,
            "subject_icon": p.subject.icon if p.subject else None,
            "goal": p.goal,
            "status": p.status,
            "program_count": len(p.programs),
            "first_program_id": p.programs[0].id if p.programs else None,
            "first_program_title": p.programs[0].title if p.programs else None,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        })
    return result


@app.post("/api/study-plans")
def create_study_plan(
    d: StudyPlanCreate,
    u: User = Depends(require_tutor_or_owner),
    db: Session = Depends(get_db)
):
    if not d.student_id and not d.group_id:
        raise HTTPException(400, "Нужен student_id или group_id")
    tutor_id = (d.tutor_id if d.tutor_id and u.role == "owner" else u.id)
    plan = StudyPlan(
        id=secrets.token_hex(6),
        tutor_id=tutor_id,
        student_id=d.student_id or None,
        group_id=d.group_id or None,
        subject_id=d.subject_id or None,
        goal=d.goal or None,
    )
    db.add(plan); db.commit(); db.refresh(plan)
    # Автоматически создаём CourseInstance (1:1 с планом)
    GOAL_LBL = {'ege':'ЕГЭ','oge':'ОГЭ','olymp':'Олимпиады',
                'improve_grades':'Улучшение оценок','deepening':'Углубление',
                'extra_education':'Доп. образование'}
    subj_obj = db.query(Subject).filter(Subject.id == plan.subject_id).first() if plan.subject_id else None
    parts = [subj_obj.name if subj_obj else None,
             GOAL_LBL.get(plan.goal, plan.goal) if plan.goal else None]
    inst_title = ' · '.join(p for p in parts if p) or 'Программа'
    ci = CourseInstance(id=gen_id(), title=inst_title,
                        tutor_id=plan.tutor_id,
                        subject_id=plan.subject_id,
                        goal=plan.goal,
                        study_plan_id=plan.id)
    db.add(ci); db.flush()
    if plan.student_id:
        enr = Enrollment(id=gen_id(), instance_id=ci.id, student_id=plan.student_id)
        db.add(enr)
    db.commit()

    return {"id": plan.id, "ok": True}


@app.get("/api/study-plans/{pid}")
def get_study_plan(
    pid: str,
    u: User = Depends(require_tutor_or_owner),
    db: Session = Depends(get_db)
):
    plan = db.query(StudyPlan).filter(StudyPlan.id == pid).first()
    if not plan: raise HTTPException(404)
    programs = []
    for prog in plan.programs:
        enr_count = db.query(Enrollment).filter(Enrollment.instance_id == prog.id).count()
        programs.append({
            "id": prog.id,
            "title": prog.title,
            "subject_id": prog.subject_id,
            "created_at": prog.created_at.isoformat() if prog.created_at else None,
            "enrollment_count": enr_count,
        })
    return {
        "id": plan.id,
        "tutor_id": plan.tutor_id,
        "tutor_name": plan.tutor.name if plan.tutor else None,
        "student_id": plan.student_id,
        "student_name": plan.student.name if plan.student else None,
        "group_id": plan.group_id,
        "group_name": plan.group.name if plan.group else None,
        "subject_id": plan.subject_id,
        "subject_name": plan.subject.name if plan.subject else None,
        "subject_icon": plan.subject.icon if plan.subject else None,
        "goal": plan.goal,
        "status": plan.status,
        "programs": programs,
        "created_at": plan.created_at.isoformat() if plan.created_at else None,
    }


@app.patch("/api/study-plans/{pid}")
def update_study_plan(
    pid: str,
    d: StudyPlanUpdate,
    u: User = Depends(require_tutor_or_owner),
    db: Session = Depends(get_db)
):
    plan = db.query(StudyPlan).filter(StudyPlan.id == pid).first()
    if not plan: raise HTTPException(404)
    if u.role != "owner" and plan.tutor_id != u.id:
        raise HTTPException(403)
    if d.subject_id is not None: plan.subject_id = d.subject_id or None
    if d.goal is not None: plan.goal = d.goal or None
    if d.status is not None: plan.status = d.status
    db.commit()
    return {"ok": True}


@app.delete("/api/study-plans/{pid}")
def delete_study_plan(
    pid: str,
    u: User = Depends(require_tutor_or_owner),
    db: Session = Depends(get_db)
):
    plan = db.query(StudyPlan).filter(StudyPlan.id == pid).first()
    if not plan: raise HTTPException(404)
    if u.role != "owner" and plan.tutor_id != u.id:
        raise HTTPException(403)
    db.delete(plan); db.commit()
    return {"ok": True}


@app.post("/api/study-plans/{pid}/programs/{instance_id}")
def link_program_to_plan(
    pid: str,
    instance_id: str,
    u: User = Depends(require_tutor_or_owner),
    db: Session = Depends(get_db)
):
    """Привязать программу (CourseInstance) к плану занятий."""
    plan = db.query(StudyPlan).filter(StudyPlan.id == pid).first()
    if not plan: raise HTTPException(404, "План не найден")
    if u.role != "owner" and plan.tutor_id != u.id:
        raise HTTPException(403)
    inst = db.query(CourseInstance).filter(CourseInstance.id == instance_id).first()
    if not inst: raise HTTPException(404, "Программа не найдена")
    inst.study_plan_id = pid
    db.commit()
    return {"ok": True}


@app.delete("/api/study-plans/{pid}/programs/{instance_id}")
def unlink_program_from_plan(
    pid: str,
    instance_id: str,
    u: User = Depends(require_tutor_or_owner),
    db: Session = Depends(get_db)
):
    """Отвязать программу от плана занятий."""
    inst = db.query(CourseInstance).filter(
        CourseInstance.id == instance_id,
        CourseInstance.study_plan_id == pid
    ).first()
    if not inst: raise HTTPException(404)
    inst.study_plan_id = None
    db.commit()
    return {"ok": True}
    study_plan_id: Optional[str] = None

