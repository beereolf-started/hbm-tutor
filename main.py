from fastapi import FastAPI,Depends,HTTPException,UploadFile,File,WebSocket,WebSocketDisconnect,Request,Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session,joinedload
import os,json,jwt,secrets
from datetime import datetime,timezone
from collections import defaultdict
from database import get_db,SessionLocal
from models import *
from schemas import *
from auth import (hash_password,verify_password,create_token,get_current_user,
    require_owner,require_tutor_or_owner,decode_token,SECRET_KEY,ALGORITHM)

app=FastAPI(title="HBM Репетитор API",version="2.0")
app.add_middleware(CORSMiddleware,allow_origins=["*"],allow_methods=["*"],allow_headers=["*"])
BASE_DIR=os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR=os.path.join(BASE_DIR,"uploads"); os.makedirs(UPLOAD_DIR,exist_ok=True)
def _up(aid,ext): return os.path.join(UPLOAD_DIR,f"{aid}{ext}"),f"uploads/{aid}{ext}"
def is_tr(u): return u.role in ("owner","tutor")

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
        student_id=u.student_id,subject_id=u.subject_id,created_at=u.created_at,subject_ids=sids)

@app.get("/api/users",response_model=list[UserOut])
def list_users(u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    if u.role=="owner": users=db.query(User).order_by(User.created_at.desc()).all()
    else:
        stids=_tutor_student_ids(u.id,db)
        student_uids=[r.id for r in db.query(User.id).filter(User.student_id.in_(stids)).all()] if stids else []
        parent_ids=[r.parent_id for r in db.execute(parent_student_link.select().where(parent_student_link.c.student_id.in_(stids))).fetchall()] if stids else []
        visible=set(student_uids)|set(parent_ids)|{u.id}
        users=db.query(User).filter(User.id.in_(visible)).order_by(User.created_at.desc()).all()
    return [_user_out(usr,db) for usr in users]

@app.post("/api/users",response_model=UserOut,status_code=201)
def create_user(d:UserCreate,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    if d.role=="tutor" and u.role!="owner": raise HTTPException(403,"Только владелец может создавать преподавателей")
    if d.role not in ("tutor","student","parent"): raise HTTPException(400,"Роль: tutor/student/parent")
    if db.query(User).filter(User.login==d.login).first(): raise HTTPException(409,"Логин занят")
    if len(d.password)<6: raise HTTPException(400,"Минимум 6 символов")
    # Tutor can only link parent to their own students
    if d.role=="parent" and d.children_ids and u.role=="tutor":
        allowed=set(_tutor_student_ids(u.id,db))
        for sid in d.children_ids:
            if sid not in allowed: raise HTTPException(403,"Нет доступа к этому ученику")
    nu=User(login=d.login,password_hash=hash_password(d.password),role=d.role,name=d.name,must_change_password=True)
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
    db.commit(); db.refresh(nu); return nu

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
    if t.role=="tutor" and u.role!="owner": raise HTTPException(403,"Только владелец может удалять преподавателей")
    if u.role=="tutor":
        # Tutor can only delete users visible to them
        stids=_tutor_student_ids(u.id,db)
        student_uids={r.id for r in db.query(User.id).filter(User.student_id.in_(stids)).all()} if stids else set()
        parent_ids={r.parent_id for r in db.execute(parent_student_link.select().where(parent_student_link.c.student_id.in_(stids))).fetchall()} if stids else set()
        if t.id not in student_uids|parent_ids: raise HTTPException(403,"Нет доступа")
    db.delete(t); db.commit()

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
    if it.file_path and os.path.exists(it.file_path): os.remove(it.file_path)
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
    if sb.file_path and os.path.exists(sb.file_path): os.remove(sb.file_path)
    db.delete(sb); db.commit()

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
    if sb.file_path and os.path.exists(sb.file_path): os.remove(sb.file_path)
    db.delete(sb); db.commit()

# ═══ ACCESS CHECK ═══
def chk_acc(stid,u,db):
    if u.role=="owner": return
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
    s=Student(name=d.name,grade=d.grade,goal=d.goal,base_rate=d.base_rate,format=d.format,subject_id=d.subject_id,created_by=u.id)
    db.add(s); db.flush()
    if u.role=="tutor": db.execute(tutor_student_link.insert().values(tutor_id=u.id,student_id=s.id))
    db.commit(); db.refresh(s); return s

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
    _cln_stu(s,db); db.delete(s); db.commit()

@app.post("/api/students/{stid}/assign-tutor/{tid}",status_code=200)
def assign_tutor(stid:str,tid:str,o:User=Depends(require_owner),db:Session=Depends(get_db)):
    if not db.query(Student).filter(Student.id==stid).first(): raise HTTPException(404)
    if not db.query(User).filter(User.id==tid,User.role=="tutor").first(): raise HTTPException(404)
    if not db.execute(tutor_student_link.select().where((tutor_student_link.c.tutor_id==tid)&(tutor_student_link.c.student_id==stid))).first():
        db.execute(tutor_student_link.insert().values(tutor_id=tid,student_id=stid)); db.commit()
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
    if it.type=="hw": raise HTTPException(400,"ДЗ нельзя удалить")
    _cln_it(it,db); db.delete(it); db.commit()

@app.post("/api/sections/{sid}/items/reorder",status_code=200)
def reorder(sid:str,ids:list[str],u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    m={i.id:i for i in db.query(Item).filter(Item.section_id==sid).all()}
    for p,iid in enumerate(ids):
        if iid in m: m[iid].position=p
    db.commit(); return {"ok":True}

# ═══ ATTACHMENTS ═══
@app.post("/api/items/{iid}/attachments",response_model=AttachmentOut,status_code=201)
async def upload_att(iid:str,file:UploadFile=File(...),u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    it=db.query(Item).filter(Item.id==iid).first()
    if not it: raise HTTPException(404)
    aid=gen_id(); ext=os.path.splitext(file.filename)[1] if file.filename else ""
    content=await file.read(); fp,dbp=_up(aid,ext)
    if len(content)>50*1024*1024: raise HTTPException(413)
    with open(fp,"wb") as f: f.write(content)
    att=Attachment(id=aid,item_id=iid,name=file.filename or "file",mime=file.content_type or "application/octet-stream",size=len(content),file_path=dbp)
    db.add(att); db.commit(); db.refresh(att); return att

@app.delete("/api/attachments/{aid}",status_code=204)
def del_att(aid:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    a=db.query(Attachment).filter(Attachment.id==aid).first()
    if not a: raise HTTPException(404)
    if a.file_path and os.path.exists(a.file_path): os.remove(a.file_path)
    db.delete(a); db.commit()

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
    for a in it.attachments:
        if a.file_path and os.path.exists(a.file_path): os.remove(a.file_path)
    for sb in it.subblocks:
        if sb.file_path and os.path.exists(sb.file_path): os.remove(sb.file_path)
def _cln_sec(sec,db):
    for it in db.query(Item).filter(Item.section_id==sec.id).all(): _cln_it(it,db)
def _cln_stu(st,db):
    for sec in db.query(Section).filter(Section.student_id==st.id).all(): _cln_sec(sec,db)

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
            seen[partner_id]={"partner_id":partner_id,"partner_name":partner.name if partner else partner_id,
                "last_text":m.text[:80],"last_at":m.created_at.isoformat() if m.created_at else None,
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
    return {
        "unread_messages":unread_msgs,
        "unread_notifications":unread_notifs,
        "unread":unread_msgs+unread_notifs,
        "notifications":[{"id":n.id,"text":n.text,"is_read":n.is_read,
            "created_at":n.created_at.isoformat() if n.created_at else None,
            "link":n.link,"notif_type":n.notif_type} for n in notifs]}

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
    owner=db.query(User).filter(User.role=="owner").first()
    if owner:
        label="логина" if d.req_type=="login" else "пароля"
        n=Notification(user_id=owner.id,text=f"Пользователь {u.name} запросил смену {label}",
            notif_type="change_request",link=f"/profile.html?uid={u.id}")
        db.add(n)
    db.commit(); return {"ok":True}

@app.get("/api/change-requests")
def list_change_requests(u:User=Depends(require_owner),db:Session=Depends(get_db)):
    reqs=db.query(ChangeRequest).filter(ChangeRequest.status=="pending").order_by(ChangeRequest.created_at.desc()).all()
    return [{"id":r.id,"user_id":r.user_id,"user_name":r.req_user.name,"req_type":r.req_type,
             "new_value":r.new_value if r.req_type=="login" else "••••••",
             "created_at":r.created_at.isoformat() if r.created_at else None} for r in reqs]

@app.post("/api/change-requests/{rid}/approve")
def approve_change_request(rid:str,u:User=Depends(require_owner),db:Session=Depends(get_db)):
    r=db.query(ChangeRequest).filter(ChangeRequest.id==rid).first()
    if not r: raise HTTPException(404)
    t=db.query(User).filter(User.id==r.user_id).first()
    if not t: raise HTTPException(404)
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
def reject_change_request(rid:str,u:User=Depends(require_owner),db:Session=Depends(get_db)):
    r=db.query(ChangeRequest).filter(ChangeRequest.id==rid).first()
    if not r: raise HTTPException(404)
    t=db.query(User).filter(User.id==r.user_id).first()
    r.status="rejected"
    if t:
        label="логина" if r.req_type=="login" else "пароля"
        n=Notification(user_id=t.id,text=f"Ваш запрос на смену {label} отклонён",notif_type="request_rejected")
        db.add(n)
    db.commit(); return {"ok":True}

# ═══ CONTACTS ═══
@app.get("/api/contacts")
def get_contacts(u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    """Returns list of users the current user can message."""
    ids=set()
    if u.role=="owner":
        # owner sees everyone
        all_u=db.query(User).filter(User.id!=u.id).all()
        return [{"id":x.id,"name":x.name,"role":x.role,
                 "last_seen":x.last_seen.isoformat() if x.last_seen else None} for x in all_u]
    elif u.role=="tutor":
        stids=set(_tutor_student_ids(u.id,db))
        # students (users linked to their students)
        if stids:
            for r in db.query(User).filter(User.student_id.in_(stids),User.id!=u.id).all():
                ids.add(r.id)
            # parents of those students
            for r in db.execute(parent_student_link.select().where(parent_student_link.c.student_id.in_(stids))).fetchall():
                ids.add(r.parent_id)
        # owner
        owner=db.query(User).filter(User.role=="owner").first()
        if owner: ids.add(owner.id)
    elif u.role=="student":
        # find tutors of the linked student profile
        if u.student_id:
            st=db.query(Student).filter(Student.id==u.student_id).first()
            if st:
                if st.created_by: ids.add(st.created_by)
                for r in db.execute(tutor_student_link.select().where(tutor_student_link.c.student_id==u.student_id)).fetchall():
                    ids.add(r.tutor_id)
        owner=db.query(User).filter(User.role=="owner").first()
        if owner: ids.add(owner.id)
    elif u.role=="parent":
        # tutors of children
        child_ids=[r.student_id for r in db.execute(parent_student_link.select().where(parent_student_link.c.parent_id==u.id)).fetchall()]
        if child_ids:
            for stid in child_ids:
                st=db.query(Student).filter(Student.id==stid).first()
                if st and st.created_by: ids.add(st.created_by)
                for r in db.execute(tutor_student_link.select().where(tutor_student_link.c.student_id==stid)).fetchall():
                    ids.add(r.tutor_id)
        owner=db.query(User).filter(User.role=="owner").first()
        if owner: ids.add(owner.id)
    ids.discard(u.id)
    users=db.query(User).filter(User.id.in_(ids)).all() if ids else []
    return [{"id":x.id,"name":x.name,"role":x.role,
             "last_seen":x.last_seen.isoformat() if x.last_seen else None} for x in users]

# ═══ PERSONAL BOARDS ═══
@app.get("/api/personal-boards",response_model=list[PersonalBoardListItem])
def list_personal_boards(u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    return db.query(PersonalBoard).filter(PersonalBoard.owner_id==u.id).order_by(PersonalBoard.updated_at.desc()).all()

@app.post("/api/personal-boards",response_model=PersonalBoardOut,status_code=201)
def create_personal_board(d:PersonalBoardCreate,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    b=PersonalBoard(owner_id=u.id,title=d.title.strip()[:200] if d.title else "Новая доска")
    db.add(b); db.commit(); db.refresh(b); return b

@app.get("/api/personal-boards/{bid}",response_model=PersonalBoardOut)
def get_personal_board(bid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    b=db.query(PersonalBoard).filter(PersonalBoard.id==bid).first()
    if not b: raise HTTPException(404)
    # allow owner or shared users
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

@app.post("/api/personal-boards/{bid}/share-with/{uid}",status_code=200)
def share_board_with_user(bid:str,uid:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    b=db.query(PersonalBoard).filter(PersonalBoard.id==bid,PersonalBoard.owner_id==u.id).first()
    if not b: raise HTTPException(404)
    target=db.query(User).filter(User.id==uid).first()
    if not target: raise HTTPException(404,"Пользователь не найден")
    if not db.execute(personal_board_share.select().where(
            (personal_board_share.c.board_id==bid)&(personal_board_share.c.user_id==uid))).first():
        db.execute(personal_board_share.insert().values(board_id=bid,user_id=uid))
        db.commit()
    return {"ok":True}

@app.get("/api/personal-boards/by-token/{token}",response_model=PersonalBoardOut)
def get_board_by_token(token:str,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    b=db.query(PersonalBoard).filter(PersonalBoard.share_token==token).first()
    if not b: raise HTTPException(404)
    # auto-add to shared
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

# ═══ STATIC ═══
app.mount("/uploads",StaticFiles(directory=UPLOAD_DIR),name="uploads")
SD="static"
if os.path.isdir(SD):
    from fastapi.responses import FileResponse,HTMLResponse
    @app.get("/api.js")
    async def sjs(): return FileResponse(os.path.join(SD,"api.js"),media_type="application/javascript")
    @app.get("/{p}.html")
    async def shtml(p:str):
        fp=os.path.join(SD,f"{p}.html")
        if os.path.isfile(fp): return FileResponse(fp,media_type="text/html")
        raise HTTPException(404)
    @app.get("/")
    async def idx():
        fp=os.path.join(SD,"login.html")
        if os.path.isfile(fp): return FileResponse(fp)
        return HTMLResponse("HBM")
