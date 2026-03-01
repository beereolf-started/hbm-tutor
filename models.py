from sqlalchemy import (Column, String, Integer, Boolean, Text, DateTime, ForeignKey, Enum as PgEnum, Table)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import enum, uuid
from database import Base

class GoalType(str, enum.Enum):
    ege="ege"; olymp="olymp"; base="base"
class FormatType(str, enum.Enum):
    online="online"; offline="offline"
class ItemType(str, enum.Enum):
    topic="topic"; hw="hw"; note="note"
class TopicStatus(str, enum.Enum):
    none="none"; progress="progress"; done="done"
class ControlStatus(str, enum.Enum):
    none="none"; passed="passed"; failed="failed"
class UserRole(str, enum.Enum):
    owner="owner"; tutor="tutor"; parent="parent"; student="student"
class CourseAccess(str, enum.Enum):
    public="public"; private="private"

def gen_id(): return uuid.uuid4().hex[:12]

parent_student_link = Table("parent_student_link", Base.metadata,
    Column("parent_id", String(12), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("student_id", String(12), ForeignKey("students.id", ondelete="CASCADE"), primary_key=True))
tutor_student_link = Table("tutor_student_link", Base.metadata,
    Column("tutor_id", String(12), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("student_id", String(12), ForeignKey("students.id", ondelete="CASCADE"), primary_key=True))

class User(Base):
    __tablename__="users"
    id=Column(String(12),primary_key=True,default=gen_id); login=Column(String(100),unique=True,nullable=False)
    password_hash=Column(String(200),nullable=False); role=Column(PgEnum(UserRole),nullable=False)
    name=Column(String(200),nullable=False); must_change_password=Column(Boolean,default=True)
    created_at=Column(DateTime(timezone=True),server_default=func.now())
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
    course=relationship("Course",back_populates="sections")
    items=relationship("CourseSectionItem",back_populates="section",cascade="all, delete-orphan",order_by="CourseSectionItem.position")

class CourseSectionItem(Base):
    __tablename__="course_section_items"
    id=Column(String(12),primary_key=True,default=gen_id)
    section_id=Column(String(12),ForeignKey("course_sections.id",ondelete="CASCADE"),nullable=False)
    type=Column(PgEnum(ItemType),nullable=False,default=ItemType.topic)
    position=Column(Integer,nullable=False,default=0); name=Column(String(300),nullable=False)
    section=relationship("CourseSection",back_populates="items")

class Student(Base):
    __tablename__="students"
    id=Column(String(12),primary_key=True,default=gen_id); name=Column(String(200),nullable=False)
    grade=Column(String(20),nullable=False,default="9"); goal=Column(PgEnum(GoalType),nullable=False,default=GoalType.ege)
    base_rate=Column(Integer,nullable=False,default=1500); format=Column(PgEnum(FormatType),nullable=False,default=FormatType.online)
    subject_id=Column(String(12),ForeignKey("subjects.id",ondelete="SET NULL"),nullable=True)
    created_by=Column(String(12),ForeignKey("users.id",ondelete="SET NULL"),nullable=True)
    created_at=Column(DateTime(timezone=True),server_default=func.now())
    subject=relationship("Subject"); creator=relationship("User",foreign_keys=[created_by])
    sections=relationship("Section",back_populates="student",cascade="all, delete-orphan",order_by="Section.position")
    parents=relationship("User",secondary=parent_student_link,backref="children_students")
    tutors=relationship("User",secondary=tutor_student_link,backref="assigned_students")

class Section(Base):
    __tablename__="sections"
    id=Column(String(12),primary_key=True,default=gen_id)
    student_id=Column(String(12),ForeignKey("students.id",ondelete="CASCADE"),nullable=False)
    title=Column(String(300),nullable=False); position=Column(Integer,nullable=False,default=0)
    is_open=Column(Boolean,default=False); idz_enabled=Column(Boolean,default=True)
    control_enabled=Column(Boolean,default=True); idz=Column(Integer,default=0)
    control=Column(PgEnum(ControlStatus),default=ControlStatus.none)
    student=relationship("Student",back_populates="sections")
    items=relationship("Item",back_populates="section",cascade="all, delete-orphan",order_by="Item.position")

class Item(Base):
    __tablename__="items"
    id=Column(String(12),primary_key=True,default=gen_id)
    section_id=Column(String(12),ForeignKey("sections.id",ondelete="CASCADE"),nullable=False)
    type=Column(PgEnum(ItemType),nullable=False); position=Column(Integer,nullable=False,default=0)
    name=Column(String(300)); status=Column(PgEnum(TopicStatus),default=TopicStatus.none)
    total=Column(Integer); done=Column(Integer); closed=Column(Boolean,default=False)
    date=Column(String(20)); closed_date=Column(String(20)); note=Column(Text); text=Column(Text)
    section=relationship("Section",back_populates="items")
    attachments=relationship("Attachment",back_populates="item",cascade="all, delete-orphan")

class Attachment(Base):
    __tablename__="attachments"
    id=Column(String(12),primary_key=True,default=gen_id)
    item_id=Column(String(12),ForeignKey("items.id",ondelete="CASCADE"),nullable=False)
    name=Column(String(500),nullable=False); mime=Column(String(200),nullable=False)
    size=Column(Integer,nullable=False); file_path=Column(String(1000))
    item=relationship("Item",back_populates="attachments")

class Board(Base):
    __tablename__="boards"
    id=Column(String(12),primary_key=True,default=gen_id)
    student_id=Column(String(12),ForeignKey("students.id",ondelete="CASCADE"),nullable=False,unique=True)
    strokes=Column(Text,default="[]")
    created_at=Column(DateTime(timezone=True),server_default=func.now())
    updated_at=Column(DateTime(timezone=True),server_default=func.now(),onupdate=func.now())
    student=relationship("Student",backref="board")
