from fastapi import FastAPI,Depends,HTTPException,UploadFile,File,WebSocket,WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session,joinedload
import os,json,jwt
from collections import defaultdict
from database import get_db,SessionLocal
from models import *
from schemas import *
from auth import (hash_password,verify_password,create_token,get_current_user,
    require_owner,require_tutor_or_owner,decode_token,SECRET_KEY,ALGORITHM)

app=FastAPI(title="HBM Репетитор API",version="2.0")
app.add_middleware(CORSMiddleware,allow_origins=["*"],allow_methods=["*"],allow_headers=["*"])
UPLOAD_DIR="uploads"; os.makedirs(UPLOAD_DIR,exist_ok=True)
def is_tr(u): return u.role in ("owner","tutor")

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
@app.get("/api/users",response_model=list[UserOut])
def list_users(u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    if u.role=="owner": return db.query(User).order_by(User.created_at.desc()).all()
    return db.query(User).filter((User.role.in_(["student","parent"]))|(User.id==u.id)).order_by(User.created_at.desc()).all()

@app.post("/api/users",response_model=UserOut,status_code=201)
def create_user(d:UserCreate,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    if d.role=="tutor" and u.role!="owner": raise HTTPException(403,"Только владелец")
    if d.role not in ("tutor","student","parent"): raise HTTPException(400,"Роль: tutor/student/parent")
    if db.query(User).filter(User.login==d.login).first(): raise HTTPException(409,"Логин занят")
    if len(d.password)<6: raise HTTPException(400,"Минимум 6 символов")
    nu=User(login=d.login,password_hash=hash_password(d.password),role=d.role,name=d.name,must_change_password=True)
    if d.role=="tutor" and d.subject_id:
        if not db.query(Subject).filter(Subject.id==d.subject_id).first(): raise HTTPException(404)
        nu.subject_id=d.subject_id
    if d.role=="student" and d.student_id:
        if not db.query(Student).filter(Student.id==d.student_id).first(): raise HTTPException(404)
        nu.student_id=d.student_id
    db.add(nu); db.flush()
    if d.role=="parent" and d.children_ids:
        for sid in d.children_ids:
            if db.query(Student).filter(Student.id==sid).first():
                db.execute(parent_student_link.insert().values(parent_id=nu.id,student_id=sid))
    db.commit(); db.refresh(nu); return nu

@app.delete("/api/users/{uid}",status_code=204)
def del_user(uid:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    t=db.query(User).filter(User.id==uid).first()
    if not t: raise HTTPException(404)
    if t.role=="owner": raise HTTPException(400)
    if t.role=="tutor" and u.role!="owner": raise HTTPException(403)
    db.delete(t); db.commit()

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
    if u.role=="tutor": q=q.filter((Course.access=="public")|(Course.author_id==u.id))
    return [CourseListItem(id=c.id,subject_id=c.subject_id,author_id=c.author_id,title=c.title,
        description=c.description,access=c.access,author_name=c.author.name if c.author else None,
        subject_name=c.subject.name if c.subject else None,sections_count=len(c.sections),
        created_at=c.created_at) for c in q.order_by(Course.updated_at.desc()).all()]
@app.get("/api/courses/{cid}",response_model=CourseOut)
def get_course(cid:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    c=db.query(Course).options(joinedload(Course.sections).joinedload(CourseSection.items)).filter(Course.id==cid).first()
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

# Course Sections
@app.post("/api/courses/{cid}/sections",response_model=CourseSectionOut,status_code=201)
def create_csec(cid:str,d:CourseSectionCreate,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    c=db.query(Course).filter(Course.id==cid).first()
    if not c: raise HTTPException(404)
    if c.author_id!=u.id and u.role!="owner": raise HTTPException(403)
    mp=db.query(CourseSection.position).filter(CourseSection.course_id==cid).order_by(CourseSection.position.desc()).first()
    sec=CourseSection(course_id=cid,title=d.title,position=(mp[0]+1) if mp else 0,idz_enabled=d.idz_enabled,control_enabled=d.control_enabled)
    db.add(sec); db.flush()
    for i,it in enumerate(d.items): db.add(CourseSectionItem(section_id=sec.id,type=it.type,position=i,name=it.name))
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
    db.delete(it); db.commit()

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
    s=db.query(Student).options(joinedload(Student.sections).joinedload(Section.items).joinedload(Item.attachments)).filter(Student.id==stid).first()
    if not s: raise HTTPException(404)
    return s

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

# Apply course
@app.post("/api/students/{stid}/apply-course/{cid}",response_model=StudentOut)
def apply_course(stid:str,cid:str,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    chk_acc(stid,u,db); st=db.query(Student).filter(Student.id==stid).first()
    if not st: raise HTTPException(404)
    co=db.query(Course).options(joinedload(Course.sections).joinedload(CourseSection.items)).filter(Course.id==cid).first()
    if not co: raise HTTPException(404)
    if co.access=="private" and co.author_id!=u.id and u.role!="owner": raise HTTPException(403)
    for sec in st.sections: _cln_sec(sec,db)
    db.query(Section).filter(Section.student_id==stid).delete()
    for csec in co.sections:
        s=Section(student_id=stid,title=csec.title,position=csec.position,idz_enabled=csec.idz_enabled,control_enabled=csec.control_enabled)
        db.add(s); db.flush()
        for ci in csec.items: db.add(Item(section_id=s.id,type=ci.type,position=ci.position,name=ci.name,status="none"))
    db.commit(); db.refresh(st); return get_student(stid,u,db)

# ═══ SECTIONS ═══
@app.post("/api/students/{stid}/sections",response_model=SectionOut,status_code=201)
def create_sec(stid:str,d:SectionCreate,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    chk_acc(stid,u,db)
    if not db.query(Student).filter(Student.id==stid).first(): raise HTTPException(404)
    mp=db.query(Section.position).filter(Section.student_id==stid).order_by(Section.position.desc()).first()
    sec=Section(student_id=stid,title=d.title,position=(mp[0]+1) if mp else 0,idz_enabled=d.idz_enabled,control_enabled=d.control_enabled)
    db.add(sec); db.commit(); db.refresh(sec); return sec

@app.patch("/api/sections/{sid}",response_model=SectionOut)
def upd_sec(sid:str,d:SectionUpdate,u:User=Depends(get_current_user),db:Session=Depends(get_db)):
    sec=db.query(Section).filter(Section.id==sid).first()
    if not sec: raise HTTPException(404)
    ud=d.model_dump(exclude_unset=True)
    if not is_tr(u):
        if set(ud.keys())-{"is_open"}: raise HTTPException(403)
    else: chk_acc(sec.student_id,u,db)
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
def upd_item(iid:str,d:ItemUpdate,u:User=Depends(require_tutor_or_owner),db:Session=Depends(get_db)):
    it=db.query(Item).filter(Item.id==iid).first()
    if not it: raise HTTPException(404)
    sec=db.query(Section).filter(Section.id==it.section_id).first()
    if sec: chk_acc(sec.student_id,u,db)
    for f,v in d.model_dump(exclude_unset=True).items(): setattr(it,f,v)
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
    fp=os.path.join(UPLOAD_DIR,f"{aid}{ext}"); content=await file.read()
    if len(content)>50*1024*1024: raise HTTPException(413)
    with open(fp,"wb") as f: f.write(content)
    att=Attachment(id=aid,item_id=iid,name=file.filename or "file",mime=file.content_type or "application/octet-stream",size=len(content),file_path=fp)
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
def _cln_sec(sec,db):
    for it in db.query(Item).filter(Item.section_id==sec.id).all(): _cln_it(it,db)
def _cln_stu(st,db):
    for sec in db.query(Section).filter(Section.student_id==st.id).all(): _cln_sec(sec,db)

# ═══ BOARD ═══
brd_conns: dict[str,set[WebSocket]] = defaultdict(set)
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
    finally: db.close()
    brd_conns[stid].add(ws)
    await ws.send_text(json.dumps({"type":"hello","user_id":uid}))
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
    except WebSocketDisconnect: pass
    except Exception as e: print(f"[WS] {e}")
    finally:
        brd_conns[stid].discard(ws)
        if not brd_conns[stid]: del brd_conns[stid]

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
