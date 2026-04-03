from fastapi import FastAPI,Depends,HTTPException,UploadFile,File,Form,WebSocket,WebSocketDisconnect,Request,Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session,joinedload
import os,json,jwt,secrets,subprocess,asyncio,shutil,mimetypes,tempfile
from datetime import datetime,timezone
from collections import defaultdict
from database import get_db,SessionLocal,engine,Base
from models import *
from schemas import *
from auth import (hash_password,verify_password,create_token,get_current_user,
    require_owner,require_tutor_or_owner,require_teamlead_or_owner,decode_token,SECRET_KEY,ALGORITHM)

app=FastAPI(title="HBM Репетитор API",version="2.0")
app.add_middleware(GZipMiddleware,minimum_size=512)
app.add_middleware(CORSMiddleware,allow_origins=["*"],allow_methods=["*"],allow_headers=["*"])

from sqlalchemy import text as _sa_text
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
    ]
    _db=SessionLocal()
    for _sql in _steps:
        try: _db.execute(_sa_text(_sql)); _db.commit()
        except: _db.rollback()
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
BASE_DIR=os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR=os.path.join(BASE_DIR,"uploads"); os.makedirs(UPLOAD_DIR,exist_ok=True)
def _up(aid,ext): return os.path.join(UPLOAD_DIR,f"{aid}{ext}"),f"uploads/{aid}{ext}"
def _abs(rel): return os.path.join(BASE_DIR,rel.replace("/",os.sep)) if rel else None
def _rm(rel):
    fp=_abs(rel)
    if fp and os.path.exists(fp): os.remove(fp)
def is_tr(u): return u.role in ("owner","tutor","teamlead")
def is_tl(u): return u.role in ("owner","teamlead")

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
    return LoginResponse(token=create_token(u.id,u.role),role=u.role,name=u.name,must_change_password=u.must_change_password)

@app.post("/api/auth/change-password")
def change_pw(d:ChangePasswordRequest,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    if not verify_password(d.old_password,u.password_hash): raise HTTPException(400,"Неверный пароль")
    if len(d.new_password)<6: raise HTTPException(400,"Минимум 6 символов")
    u.password_hash=hash_password(d.new_password); u.must_change_password=False; db.commit()
    return {"ok":True}

@app.get("/api/auth/me",response_model=UserOut)
def me(u:User=Depends(get_current_user)): return u

# ═══ USERS ═══
def _tutor_student_ids(uid,db):
    own=[s.id for s in db.query(Student).filter(Student.created_by==uid).all()]
    linked=[r.student_id for r in db.execute(tutor_student_link.select().where(tutor_student_link.c.tutor_id==uid)).fetchall()]
    return list(set(own+linked))

def _user_out(u,db):
    sids=[r.subject_id for r in db.execute(tutor_subject_link.select().where(tutor_subject_link.c.tutor_id==u.id)).fetchall()]
    return UserOut(id=u.id,login=u.login,role=u.role,name=u.name,must_change_password=u.must_change_password,
        student_id=u.student_id,subject_id=u.subject_id,created_at=u.created_at,subject_ids=sids,
        teamlead_id=u.teamlead_id,no_commission=bool(u.no_commission))

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
    if d.role=="tutor" and u.role not in ("owner","teamlead"): raise HTTPException(403,"Только владелец или teamlead может создавать преподавателей")
    if d.role=="board_user" and u.role!="owner": raise HTTPException(403,"Только владелец может создавать пользователей доски")
    if d.role=="teamlead" and u.role!="owner": raise HTTPException(403,"Только владелец может создавать teamlead")
    if d.role not in ("tutor","student","parent","board_user","teamlead"): raise HTTPException(400,"Роль: tutor/student/parent/board_user/teamlead")
    if db.query(User).filter(User.login==d.login).first(): raise HTTPException(409,"Логин занят")
    if len(d.password)<6: raise HTTPException(400,"Минимум 6 символов")
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
    if u.role=="tutor": q=q.filter((Course.access.in_(["public","internal"]))|(Course.author_id==u.id))
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
def list_students(u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    if u.role=="owner": return db.query(Student).order_by(Student.created_at.desc()).all()
    if u.role=="teamlead":
        sids=_teamlead_student_ids(u.id,db)
        return db.query(Student).filter(Student.id.in_(sids)).order_by(Student.created_at.desc()).all() if sids else []
    if u.role=="tutor":
        own=db.query(Student).filter(Student.created_by==u.id)
        lids=[r.student_id for r in db.execute(tutor_student_link.select().where(tutor_student_link.c.tutor_id==u.id)).fetchall()]
        return own.union(db.query(Student).filter(Student.id.in_(lids))).all() if lids else own.all()
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

@app.post("/api/students",response_model=StudentOut,status_code=201)
def create_student(d:StudentCreate,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    s=Student(name=d.name,level=d.level,grade=d.grade,goal=d.goal,base_rate=d.base_rate,format=d.format,subject_id=d.subject_id,created_by=u.id)
    db.add(s); db.flush()
    if u.role=="tutor": db.execute(tutor_student_link.insert().values(tutor_id=u.id,student_id=s.id))
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

# ═══ GLOBAL USER CONNECTIONS (for calls / notifications) ═══
user_global_conns: dict[str, WebSocket] = {}  # uid -> ws

async def _send_to_user(uid: str, msg: dict):
    ws = user_global_conns.get(uid)
    if ws:
        try: await ws.send_text(json.dumps(msg))
        except: pass

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
    user_global_conns[uid] = ws
    try:
        while True:
            raw = await ws.receive_text()
            try: msg = json.loads(raw)
            except: continue
            mtype = msg.get("type","")
            # Call signaling: forward to target user
            if mtype in ("call_offer","call_answer","call_ice","call_reject","call_end","call_busy"):
                to_uid = msg.get("to")
                if to_uid:
                    msg["from"] = uid
                    if user_global_conns.get(to_uid):
                        await _send_to_user(to_uid, msg)
                    elif mtype == "call_offer":
                        await ws.send_text(json.dumps({"type":"call_unavailable","to":to_uid}))
    except WebSocketDisconnect: pass
    except Exception as e: print(f"[USER_WS] {e}")
    finally:
        if user_global_conns.get(uid) is ws:
            del user_global_conns[uid]

# ═══ BOARD ═══
brd_conns: dict[str,set[WebSocket]] = defaultdict(set)
brd_users: dict[str,dict[str,str]] = defaultdict(dict)  # stid -> {uid: name}

def _get_board(stid,db):
    b=db.query(Board).filter(Board.student_id==stid).first()
    if not b: b=Board(student_id=stid,strokes="[]"); db.add(b); db.commit(); db.refresh(b)
    return b

@app.get("/api/boards/{stid}",response_model=BoardOut)
def get_board(stid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    chk_acc(stid,u,db)
    if not db.query(Student).filter(Student.id==stid).first(): raise HTTPException(404)
    return _get_board(stid,db)

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
            b=_get_board(stid,db); b.strokes=json.dumps(strokes_list); db.commit()
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
    if not token: await ws.close(code=4001); return
    try: payload=jwt.decode(token,SECRET_KEY,algorithms=[ALGORITHM])
    except: await ws.close(code=4001); return
    uid=payload.get("sub"); urole=payload.get("role")
    db=SessionLocal()
    try:
        user=db.query(User).filter(User.id==uid).first()
        if not user: await ws.close(code=4001); return
        if user.role=="student" and user.student_id!=stid: await ws.close(code=4003); return
        if user.role=="parent":
            if not db.execute(parent_student_link.select().where((parent_student_link.c.parent_id==user.id)&(parent_student_link.c.student_id==stid))).first():
                await ws.close(code=4003); return
        if user.role=="tutor":
            st=db.query(Student).filter(Student.id==stid).first()
            if st and st.created_by!=user.id:
                if not db.execute(tutor_student_link.select().where((tutor_student_link.c.tutor_id==user.id)&(tutor_student_link.c.student_id==stid))).first():
                    await ws.close(code=4003); return
        _get_board(stid,db)
        uname_board=user.name
    finally: db.close()
    brd_conns[stid].add(ws)
    brd_users[stid][uid]=uname_board
    # notify others about join
    await _bcast(stid,json.dumps({"type":"user_join","uid":uid,"name":uname_board,"online":list({"uid":k,"name":v} for k,v in brd_users[stid].items())}),ws)
    await ws.send_text(json.dumps({"type":"hello","user_id":uid,"name":uname_board,"online":[{"uid":k,"name":v} for k,v in brd_users[stid].items() if k!=uid]}))
    try:
        while True:
            raw=await ws.receive_text()
            try: msg=json.loads(raw)
            except: continue
            mt=msg.get("type")
            if mt=="load":
                db=SessionLocal()
                try: db.expire_all(); b=db.query(Board).filter(Board.student_id==stid).first(); sj=b.strokes if b else "[]"
                finally: db.close()
                await ws.send_text(json.dumps({"type":"strokes","data":json.loads(sj)}))
            elif mt=="stroke":
                sd=msg.get("data",{})
                if not sd.get("user_id"): sd["user_id"]=uid
                db=SessionLocal()
                try: b=_get_board(stid,db); c=json.loads(b.strokes); c.append(sd); b.strokes=json.dumps(c); db.commit()
                finally: db.close()
                await _bcast(stid,json.dumps({"type":"stroke","data":sd}),ws)
            elif mt=="clear":
                if urole not in ("owner","tutor"): continue
                db=SessionLocal()
                try:
                    b=db.query(Board).filter(Board.student_id==stid).first()
                    if b: b.strokes="[]"; db.commit()
                finally: db.close()
                await _bcast(stid,json.dumps({"type":"clear"}),ws)
            elif mt=="undo":
                db=SessionLocal(); rid=None
                try:
                    b=_get_board(stid,db); c=json.loads(b.strokes)
                    for i in range(len(c)-1,-1,-1):
                        if c[i].get("user_id")==uid: rid=c[i].get("id"); c.pop(i); break
                    if rid: b.strokes=json.dumps(c); db.commit()
                finally: db.close()
                if rid:
                    await ws.send_text(json.dumps({"type":"erase_stroke","id":rid}))
                    await _bcast(stid,json.dumps({"type":"erase_stroke","id":rid}),ws)
            elif mt=="erase_stroke":
                eid=msg.get("id")
                if not eid: continue
                db=SessionLocal()
                try: b=_get_board(stid,db); c=json.loads(b.strokes); b.strokes=json.dumps([s for s in c if s.get("id")!=eid]); db.commit()
                finally: db.close()
                await _bcast(stid,json.dumps({"type":"erase_stroke","id":eid}),ws)
            elif mt=="stroke_update":
                sd=msg.get("data",{})
                eid=sd.get("id")
                if not eid: continue
                if not sd.get("user_id"): sd["user_id"]=uid
                db=SessionLocal()
                try:
                    b=_get_board(stid,db); c=json.loads(b.strokes)
                    idx=next((i for i,s in enumerate(c) if s.get("id")==eid),-1)
                    if idx>=0: c[idx]=sd
                    else: c.append(sd)
                    b.strokes=json.dumps(c); db.commit()
                finally: db.close()
                await _bcast(stid,json.dumps({"type":"stroke_update","data":sd}),ws)
            elif mt in ("cursor","view"):
                msg["uid"]=uid; msg["name"]=uname_board
                await _bcast(stid,json.dumps(msg),ws)
    except WebSocketDisconnect: pass
    except Exception as e: print(f"[WS] {e}")
    finally:
        brd_conns[stid].discard(ws)
        brd_users[stid].pop(uid,None)
        if not brd_conns[stid]:
            del brd_conns[stid]
            if stid in brd_users: del brd_users[stid]
        await _bcast(stid,json.dumps({"type":"user_leave","uid":uid}),None)

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
    return MessageOut(id=msg.id,from_id=msg.from_id,to_id=msg.to_id,text=msg.text,
        is_read=msg.is_read,created_at=msg.created_at,
        from_name=u.name,to_name=to.name)

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
            "subject_ids":sids,"courses":courses}
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
        if len(d.password)<6: raise HTTPException(400,"Минимум 6 символов")
        t.password_hash=hash_password(d.password); t.must_change_password=False
        n=Notification(user_id=t.id,text="Ваш пароль был изменён администратором",notif_type="system")
        db.add(n)
    if d.no_commission is not None: t.no_commission=d.no_commission
    db.commit(); return {"ok":True}

# ═══ CHANGE REQUESTS ═══
@app.post("/api/profile/change-request",status_code=201)
def create_change_request(d:ChangeRequestCreate,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    if d.req_type not in ("login","password"): raise HTTPException(400,"Тип: login или password")
    if d.req_type=="login":
        if not d.new_value.strip(): raise HTTPException(400,"Введите логин")
        if db.query(User).filter(User.login==d.new_value,User.id!=u.id).first(): raise HTTPException(409,"Логин уже занят")
    if d.req_type=="password" and len(d.new_value)<6: raise HTTPException(400,"Минимум 6 символов")
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
def get_schedule(u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    slots=db.query(ScheduleSlot).options(joinedload(ScheduleSlot.student)).filter(ScheduleSlot.tutor_id==u.id).all()
    return [{"id":s.id,"tutor_id":s.tutor_id,"student_id":s.student_id,
             "student_name":s.student.name if s.student else None,
             "day_of_week":s.day_of_week,"slot_index":s.slot_index,
             "duration":s.duration,"note":s.note,"color":s.color} for s in slots]

@app.post("/api/schedule",status_code=200)
def set_schedule_slot(d:ScheduleSlotCreate,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    if d.day_of_week not in range(7) or d.slot_index not in range(48): raise HTTPException(400,"Некорректные данные")
    existing=db.query(ScheduleSlot).filter(
        ScheduleSlot.tutor_id==u.id,ScheduleSlot.day_of_week==d.day_of_week,ScheduleSlot.slot_index==d.slot_index
    ).first()
    dur=max(1,min(d.duration,8))
    if existing:
        existing.student_id=d.student_id; existing.duration=dur; existing.note=d.note; existing.color=d.color
        db.commit(); db.refresh(existing); slot=existing
    else:
        slot=ScheduleSlot(tutor_id=u.id,student_id=d.student_id,day_of_week=d.day_of_week,
                          slot_index=d.slot_index,duration=dur,note=d.note,color=d.color)
        db.add(slot); db.commit(); db.refresh(slot)
    st=db.query(Student).filter(Student.id==slot.student_id).first() if slot.student_id else None
    return {"id":slot.id,"tutor_id":slot.tutor_id,"student_id":slot.student_id,
            "student_name":st.name if st else None,
            "day_of_week":slot.day_of_week,"slot_index":slot.slot_index,
            "duration":slot.duration,"note":slot.note,"color":slot.color}

@app.delete("/api/schedule/{slot_id}",status_code=204)
def del_schedule_slot(slot_id:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    slot=db.query(ScheduleSlot).filter(ScheduleSlot.id==slot_id,ScheduleSlot.tutor_id==u.id).first()
    if not slot: raise HTTPException(404)
    db.delete(slot); db.commit()

@app.get("/api/schedule/my")
def get_my_schedule(u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    if u.role!="student" or not u.student_id: raise HTTPException(403)
    slots=db.query(ScheduleSlot).options(joinedload(ScheduleSlot.student)).filter(
        ScheduleSlot.student_id==u.student_id).order_by(ScheduleSlot.day_of_week,ScheduleSlot.slot_index).all()
    return [{"id":s.id,"tutor_id":s.tutor_id,"student_id":s.student_id,
             "day_of_week":s.day_of_week,"slot_index":s.slot_index,"duration":s.duration,
             "note":s.note,"student_note":s.student_note,"color":s.color} for s in slots]

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
    slots=db.query(ScheduleSlot).filter(ScheduleSlot.student_id.in_(child_ids)).order_by(ScheduleSlot.day_of_week,ScheduleSlot.slot_index).all()
    students={s.id:s for s in db.query(Student).filter(Student.id.in_(child_ids)).all()}
    return [{"id":s.id,"tutor_id":s.tutor_id,"student_id":s.student_id,
             "student_name":students[s.student_id].name if s.student_id in students else "?",
             "day_of_week":s.day_of_week,"slot_index":s.slot_index,"duration":s.duration,
             "note":s.note,"student_note":s.student_note,"color":s.color} for s in slots]

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
        if len(new_pw)<6: raise HTTPException(400,"Минимум 6 символов")
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

# ═══ LESSON RECORDS ═══

@app.get("/api/lessons")
def list_lessons(u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db),
                 tutor_id:str=None,student_id:str=None,date_from:str=None,date_to:str=None):
    from sqlalchemy import and_
    q=db.query(LessonRecord)
    if u.role=="tutor": q=q.filter(LessonRecord.tutor_id==u.id)
    elif u.role=="teamlead":
        tids=_team_tutor_ids(u.id,db)
        q=q.filter(LessonRecord.tutor_id.in_(tids))
        if tutor_id and tutor_id in tids: q=q.filter(LessonRecord.tutor_id==tutor_id)
    else: # owner
        if tutor_id: q=q.filter(LessonRecord.tutor_id==tutor_id)
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
    records=q.order_by(LessonRecord.held_at.desc()).limit(500).all()
    result=[]
    for r in records:
        tutor=db.query(User).filter(User.id==r.tutor_id).first()
        st=db.query(Student).filter(Student.id==r.student_id).first()
        result.append({"id":r.id,"tutor_id":r.tutor_id,"tutor_name":tutor.name if tutor else None,
                       "student_id":r.student_id,"student_name":st.name if st else None,
                       "held_at":r.held_at.isoformat() if r.held_at else None,
                       "duration_min":r.duration_min,"rate":r.rate,"amount":r.amount,
                       "note":r.note,"slot_id":r.slot_id})
    return result

@app.post("/api/lessons",status_code=201)
def create_lesson(d:dict=Body(...),u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    tutor_id=d.get("tutor_id",u.id)
    if u.role=="tutor" and tutor_id!=u.id: raise HTTPException(403,"Тьютор может логировать только свои занятия")
    if u.role=="teamlead":
        tids=_team_tutor_ids(u.id,db)
        if tutor_id not in tids: raise HTTPException(403,"Тьютор не в вашей команде")
    tutor=db.query(User).filter(User.id==tutor_id).first()
    if not tutor: raise HTTPException(404,"Тьютор не найден")
    student_id=d.get("student_id")
    if not student_id or not db.query(Student).filter(Student.id==student_id).first(): raise HTTPException(404,"Ученик не найден")
    from datetime import datetime as _dt
    held_at_str=d.get("held_at")
    held_at=_dt.fromisoformat(held_at_str) if held_at_str else _dt.now(timezone.utc)
    rate=int(d.get("rate",1500)); duration_min=int(d.get("duration_min",60))
    lr=LessonRecord(id=gen_id(),tutor_id=tutor_id,student_id=student_id,held_at=held_at,
                    duration_min=duration_min,rate=rate,amount=rate,
                    note=d.get("note"),slot_id=d.get("slot_id"),created_by=u.id)
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
    return {"total_lessons":len(records),"total_amount":total_amount,"total_commission":total_commission,
            "by_tutor":list(by_tutor.values()),"by_week":sorted(by_week.values(),key=lambda x:x["week"])}

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
        fp=os.path.join(SD,"login.html")
        if os.path.isfile(fp): return FileResponse(fp)
        return HTMLResponse("HBM")
