from sqlalchemy import (Column, String, Integer, Boolean, Text, DateTime, ForeignKey, Enum as PgEnum, Table)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import enum, uuid
from database import Base

class GoalType(str, enum.Enum):
    ege="ege"; oge="oge"; olymp="olymp"
    improve_grades="improve_grades"; deepening="deepening"; extra_education="extra_education"
class StudentLevelType(str, enum.Enum):
    school="school"; university="university"; additional="additional"
class FormatType(str, enum.Enum):
    online="online"; offline="offline"
class ItemType(str, enum.Enum):
    topic="topic"; hw="hw"; note="note"; media="media"; control="control"; idz="idz"; code="code"
class TopicStatus(str, enum.Enum):
    none="none"; progress="progress"; done="done"
class ControlStatus(str, enum.Enum):
    none="none"; passed="passed"; failed="failed"
class UserRole(str, enum.Enum):
    owner="owner"; tutor="tutor"; parent="parent"; student="student"; board_user="board_user"
class CourseAccess(str, enum.Enum):
    public="public"; internal="internal"; private="private"

def gen_id(): return uuid.uuid4().hex[:12]

parent_student_link = Table("parent_student_link", Base.metadata,
    Column("parent_id", String(12), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("student_id", String(12), ForeignKey("students.id", ondelete="CASCADE"), primary_key=True))
tutor_student_link = Table("tutor_student_link", Base.metadata,
    Column("tutor_id", String(12), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("student_id", String(12), ForeignKey("students.id", ondelete="CASCADE"), primary_key=True))
tutor_subject_link = Table("tutor_subjects", Base.metadata,
    Column("tutor_id", String(12), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("subject_id", String(12), ForeignKey("subjects.id", ondelete="CASCADE"), primary_key=True))

class User(Base):
    __tablename__="users"
    id=Column(String(12),primary_key=True,default=gen_id); login=Column(String(100),unique=True,nullable=False)
    password_hash=Column(String(200),nullable=False); role=Column(PgEnum(UserRole),nullable=False)
    name=Column(String(200),nullable=False); must_change_password=Column(Boolean,default=True)
    created_at=Column(DateTime(timezone=True),server_default=func.now())
    last_seen=Column(DateTime(timezone=True),nullable=True)
    about=Column(Text,nullable=True)
    photo=Column(Text,nullable=True)
    owner_notes=Column(Text,nullable=True)
    student_id=Column(String(12),ForeignKey("students.id",ondelete="SET NULL"),nullable=True)
    student_profile=relationship("Student",foreign_keys=[student_id])
    subject_id=Column(String(12),ForeignKey("subjects.id",ondelete="SET NULL"),nullable=True)
    subject=relationship("Subject",back_populates="tutors")

class Subject(Base):
    __tablename__="subjects"
    id=Column(String(12),primary_key=True,default=gen_id); name=Column(String(200),unique=True,nullable=False)
    icon=Column(String(10),default="📐"); created_at=Column(DateTime(timezone=True),server_default=func.now())
    tutors=relationship("User",back_populates="subject")
    courses=relationship("Course",back_populates="subject",cascade="all, delete-orphan")

class Course(Base):
    __tablename__="courses"
    id=Column(String(12),primary_key=True,default=gen_id)
    subject_id=Column(String(12),ForeignKey("subjects.id",ondelete="CASCADE"),nullable=False)
    author_id=Column(String(12),ForeignKey("users.id",ondelete="CASCADE"),nullable=False)
    title=Column(String(300),nullable=False); description=Column(Text,default="")
    access=Column(PgEnum(CourseAccess),default=CourseAccess.public)
    created_at=Column(DateTime(timezone=True),server_default=func.now())
    updated_at=Column(DateTime(timezone=True),server_default=func.now(),onupdate=func.now())
    subject=relationship("Subject",back_populates="courses")
    author=relationship("User",foreign_keys=[author_id])
    sections=relationship("CourseSection",back_populates="course",cascade="all, delete-orphan",order_by="CourseSection.position")

class CourseSection(Base):
    __tablename__="course_sections"
    id=Column(String(12),primary_key=True,default=gen_id)
    course_id=Column(String(12),ForeignKey("courses.id",ondelete="CASCADE"),nullable=False)
    title=Column(String(300),nullable=False); position=Column(Integer,nullable=False,default=0)
    idz_enabled=Column(Boolean,default=True); control_enabled=Column(Boolean,default=True)
    idz_text=Column(Text,nullable=True)
    course=relationship("Course",back_populates="sections")
    items=relationship("CourseSectionItem",back_populates="section",cascade="all, delete-orphan",order_by="CourseSectionItem.position")

class CourseSectionItem(Base):
    __tablename__="course_section_items"
    id=Column(String(12),primary_key=True,default=gen_id)
    section_id=Column(String(12),ForeignKey("course_sections.id",ondelete="CASCADE"),nullable=False)
    type=Column(String(50),nullable=False,default="topic")
    position=Column(Integer,nullable=False,default=0); name=Column(String(300),nullable=False,default="")
    total=Column(Integer,nullable=True); text=Column(Text,nullable=True); note=Column(Text,nullable=True); lang=Column(String(20),nullable=True)
    file_path=Column(String(1000),nullable=True); mime=Column(String(200),nullable=True); size=Column(Integer,nullable=True)
    section=relationship("CourseSection",back_populates="items")
    subblocks=relationship("CourseItemSubblock",back_populates="item",cascade="all,delete-orphan",order_by="CourseItemSubblock.position")

class Student(Base):
    __tablename__="students"
    id=Column(String(12),primary_key=True,default=gen_id); name=Column(String(200),nullable=False)
    level=Column(PgEnum(StudentLevelType),nullable=False,default=StudentLevelType.school)
    grade=Column(String(20),nullable=True,default="9"); goal=Column(PgEnum(GoalType),nullable=False,default=GoalType.ege)
    base_rate=Column(Integer,nullable=False,default=1500); format=Column(PgEnum(FormatType),nullable=False,default=FormatType.online)
    rewards_enabled=Column(Boolean,default=True,server_default='true')
    subject_id=Column(String(12),ForeignKey("subjects.id",ondelete="SET NULL"),nullable=True)
    created_by=Column(String(12),ForeignKey("users.id",ondelete="SET NULL"),nullable=True)
    created_at=Column(DateTime(timezone=True),server_default=func.now())
    subject=relationship("Subject"); creator=relationship("User",foreign_keys=[created_by])
    sections=relationship("Section",back_populates="student",cascade="all, delete-orphan",order_by="Section.position")
    courses=relationship("StudentCourse",back_populates="student",cascade="all, delete-orphan",order_by="StudentCourse.created_at")
    parents=relationship("User",secondary=parent_student_link,backref="children_students")
    tutors=relationship("User",secondary=tutor_student_link,backref="assigned_students")

class StudentCourse(Base):
    __tablename__="student_courses"
    id=Column(String(12),primary_key=True,default=gen_id)
    student_id=Column(String(12),ForeignKey("students.id",ondelete="CASCADE"),nullable=False)
    tutor_id=Column(String(12),ForeignKey("users.id",ondelete="SET NULL"),nullable=True)
    title=Column(String(200),nullable=False)
    created_at=Column(DateTime(timezone=True),server_default=func.now())
    student=relationship("Student",back_populates="courses")
    tutor=relationship("User",foreign_keys=[tutor_id])
    sections=relationship("Section",back_populates="course",order_by="Section.position")

class Section(Base):
    __tablename__="sections"
    id=Column(String(12),primary_key=True,default=gen_id)
    student_id=Column(String(12),ForeignKey("students.id",ondelete="CASCADE"),nullable=False)
    course_id=Column(String(12),ForeignKey("student_courses.id",ondelete="SET NULL"),nullable=True)
    title=Column(String(300),nullable=False); position=Column(Integer,nullable=False,default=0)
    is_open=Column(Boolean,default=False); idz_enabled=Column(Boolean,default=True)
    control_enabled=Column(Boolean,default=True); idz=Column(Integer,default=0)
    control=Column(PgEnum(ControlStatus),default=ControlStatus.none)
    locked=Column(Boolean,default=False,server_default='false')
    idz_text=Column(Text,nullable=True)
    student=relationship("Student",back_populates="sections")
    course=relationship("StudentCourse",back_populates="sections")
    items=relationship("Item",back_populates="section",cascade="all, delete-orphan",order_by="Item.position")

class Item(Base):
    __tablename__="items"
    id=Column(String(12),primary_key=True,default=gen_id)
    section_id=Column(String(12),ForeignKey("sections.id",ondelete="CASCADE"),nullable=False)
    type=Column(PgEnum(ItemType),nullable=False); position=Column(Integer,nullable=False,default=0)
    name=Column(String(300)); status=Column(PgEnum(TopicStatus),default=TopicStatus.none)
    total=Column(Integer); done=Column(Integer); closed=Column(Boolean,default=False)
    date=Column(String(20)); closed_date=Column(String(20)); note=Column(Text); text=Column(Text)
    grade=Column(Integer,nullable=True); student_answer=Column(Text,nullable=True)
    lang=Column(String(20),nullable=True)
    section=relationship("Section",back_populates="items")
    attachments=relationship("Attachment",back_populates="item",cascade="all, delete-orphan")
    subblocks=relationship("ItemSubblock",back_populates="item",cascade="all,delete-orphan",order_by="ItemSubblock.position")

class Attachment(Base):
    __tablename__="attachments"
    id=Column(String(12),primary_key=True,default=gen_id)
    item_id=Column(String(12),ForeignKey("items.id",ondelete="CASCADE"),nullable=False)
    name=Column(String(500),nullable=False); mime=Column(String(200),nullable=False)
    size=Column(Integer,nullable=False); file_path=Column(String(1000))
    item=relationship("Item",back_populates="attachments")

class CourseItemSubblock(Base):
    __tablename__="course_item_subblocks"
    id=Column(String(12),primary_key=True,default=gen_id)
    item_id=Column(String(12),ForeignKey("course_section_items.id",ondelete="CASCADE"),nullable=False)
    type=Column(String(10),nullable=False,default="text")
    content=Column(Text,nullable=True); name=Column(String(300),nullable=True)
    position=Column(Integer,nullable=False,default=0)
    file_path=Column(String(1000),nullable=True); mime=Column(String(200),nullable=True); size=Column(Integer,nullable=True)
    item=relationship("CourseSectionItem",back_populates="subblocks")

class ItemSubblock(Base):
    __tablename__="item_subblocks"
    id=Column(String(12),primary_key=True,default=gen_id)
    item_id=Column(String(12),ForeignKey("items.id",ondelete="CASCADE"),nullable=False)
    type=Column(String(10),nullable=False,default="text")
    content=Column(Text,nullable=True); name=Column(String(300),nullable=True)
    position=Column(Integer,nullable=False,default=0)
    file_path=Column(String(1000),nullable=True); mime=Column(String(200),nullable=True); size=Column(Integer,nullable=True)
    item=relationship("Item",back_populates="subblocks")

class Board(Base):
    __tablename__="boards"
    id=Column(String(12),primary_key=True,default=gen_id)
    student_id=Column(String(12),ForeignKey("students.id",ondelete="CASCADE"),nullable=False,unique=True)
    strokes=Column(Text,default="[]")
    created_at=Column(DateTime(timezone=True),server_default=func.now())
    updated_at=Column(DateTime(timezone=True),server_default=func.now(),onupdate=func.now())
    student=relationship("Student",backref="board")

class Message(Base):
    __tablename__="messages"
    id=Column(String(12),primary_key=True,default=gen_id)
    from_id=Column(String(12),ForeignKey("users.id",ondelete="CASCADE"),nullable=False)
    to_id=Column(String(12),ForeignKey("users.id",ondelete="CASCADE"),nullable=False)
    text=Column(Text,nullable=False)
    created_at=Column(DateTime(timezone=True),server_default=func.now())
    is_read=Column(Boolean,default=False,server_default='false')
    from_user=relationship("User",foreign_keys=[from_id])
    to_user=relationship("User",foreign_keys=[to_id])

class PersonalBoard(Base):
    __tablename__="personal_boards"
    id=Column(String(12),primary_key=True,default=gen_id)
    owner_id=Column(String(12),ForeignKey("users.id",ondelete="CASCADE"),nullable=False)
    title=Column(String(300),nullable=False,default="Новая доска")
    strokes=Column(Text,default="[]")
    share_token=Column(String(32),nullable=True,unique=True)
    created_at=Column(DateTime(timezone=True),server_default=func.now())
    updated_at=Column(DateTime(timezone=True),server_default=func.now(),onupdate=func.now())
    owner=relationship("User",foreign_keys=[owner_id])

student_platform_courses = Table("student_platform_courses", Base.metadata,
    Column("student_id", String(12), ForeignKey("students.id", ondelete="CASCADE"), primary_key=True),
    Column("course_id", String(12), ForeignKey("courses.id", ondelete="CASCADE"), primary_key=True))

personal_board_share = Table("personal_board_shares", Base.metadata,
    Column("board_id", String(12), ForeignKey("personal_boards.id", ondelete="CASCADE"), primary_key=True),
    Column("user_id", String(12), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True))

class Notification(Base):
    __tablename__="notifications"
    id=Column(String(12),primary_key=True,default=gen_id)
    user_id=Column(String(12),ForeignKey("users.id",ondelete="CASCADE"),nullable=False)
    text=Column(Text,nullable=False)
    is_read=Column(Boolean,default=False,server_default='false')
    created_at=Column(DateTime(timezone=True),server_default=func.now())
    link=Column(String(500),nullable=True)
    notif_type=Column(String(50),nullable=True)
    notif_user=relationship("User",foreign_keys=[user_id])

class ChangeRequest(Base):
    __tablename__="change_requests"
    id=Column(String(12),primary_key=True,default=gen_id)
    user_id=Column(String(12),ForeignKey("users.id",ondelete="CASCADE"),nullable=False)
    req_type=Column(String(20),nullable=False)
    new_value=Column(String(300),nullable=False)
    status=Column(String(20),default='pending')
    created_at=Column(DateTime(timezone=True),server_default=func.now())
    req_user=relationship("User",foreign_keys=[user_id])

class BoardInvite(Base):
    __tablename__="board_invites"
    id=Column(String(12),primary_key=True,default=gen_id)
    board_id=Column(String(12),ForeignKey("personal_boards.id",ondelete="CASCADE"),nullable=False)
    from_id=Column(String(12),ForeignKey("users.id",ondelete="CASCADE"),nullable=False)
    to_id=Column(String(12),ForeignKey("users.id",ondelete="CASCADE"),nullable=False)
    status=Column(String(20),default='pending',nullable=False)  # pending/accepted/declined
    created_at=Column(DateTime(timezone=True),server_default=func.now())
    board=relationship("PersonalBoard",foreign_keys=[board_id])
    from_user=relationship("User",foreign_keys=[from_id])
    to_user=relationship("User",foreign_keys=[to_id])

class ScheduleSlot(Base):
    __tablename__="schedule_slots"
    id=Column(String(12),primary_key=True,default=gen_id)
    tutor_id=Column(String(12),ForeignKey("users.id",ondelete="CASCADE"),nullable=False)
    student_id=Column(String(12),ForeignKey("students.id",ondelete="CASCADE"),nullable=True)
    day_of_week=Column(Integer,nullable=False)   # 0=Пн … 6=Вс
    slot_index=Column(Integer,nullable=False)    # 0=00:00 … 47=23:30
    duration=Column(Integer,nullable=False,default=2) # в 30-мин слотах, 2=1ч
    note=Column(String(300),nullable=True)
    student_note=Column(String(500),nullable=True)
    color=Column(String(20),nullable=True)
    created_at=Column(DateTime(timezone=True),server_default=func.now())
    tutor=relationship("User",foreign_keys=[tutor_id])
    student=relationship("Student",foreign_keys=[student_id])
