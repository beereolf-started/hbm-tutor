from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class LoginRequest(BaseModel):
    login: str; password: str
class LoginResponse(BaseModel):
    token: str; role: str; name: str; must_change_password: bool
class ChangePasswordRequest(BaseModel):
    old_password: str; new_password: str

class UserCreate(BaseModel):
    login: str; password: str; role: str; name: str
    student_id: Optional[str]=None; children_ids: list[str]=[]; subject_id: Optional[str]=None
class UserOut(BaseModel):
    id: str; login: str; role: str; name: str; must_change_password: bool
    student_id: Optional[str]=None; subject_id: Optional[str]=None; created_at: Optional[datetime]=None
    model_config = {"from_attributes": True}

class SubjectCreate(BaseModel):
    name: str; icon: str = "📐"
class SubjectUpdate(BaseModel):
    name: Optional[str]=None; icon: Optional[str]=None
class SubjectOut(BaseModel):
    id: str; name: str; icon: str; created_at: Optional[datetime]=None
    model_config = {"from_attributes": True}

class CourseSectionItemCreate(BaseModel):
    name: str; type: str = "topic"
class CourseSectionItemOut(BaseModel):
    id: str; section_id: str; type: str; position: int; name: str
    model_config = {"from_attributes": True}
class CourseSectionCreate(BaseModel):
    title: str; idz_enabled: bool=True; control_enabled: bool=True
    items: list[CourseSectionItemCreate] = []
class CourseSectionUpdate(BaseModel):
    title: Optional[str]=None; idz_enabled: Optional[bool]=None; control_enabled: Optional[bool]=None
class CourseSectionOut(BaseModel):
    id: str; course_id: str; title: str; position: int; idz_enabled: bool; control_enabled: bool
    items: list[CourseSectionItemOut] = []
    model_config = {"from_attributes": True}
class CourseCreate(BaseModel):
    subject_id: str; title: str; description: str=""; access: str="public"
class CourseUpdate(BaseModel):
    title: Optional[str]=None; description: Optional[str]=None; access: Optional[str]=None
class CourseOut(BaseModel):
    id: str; subject_id: str; author_id: str; title: str; description: str; access: str
    created_at: Optional[datetime]=None; updated_at: Optional[datetime]=None
    sections: list[CourseSectionOut] = []
    model_config = {"from_attributes": True}
class CourseListItem(BaseModel):
    id: str; subject_id: str; author_id: str; title: str; description: str; access: str
    author_name: Optional[str]=None; subject_name: Optional[str]=None
    sections_count: int=0; created_at: Optional[datetime]=None
    model_config = {"from_attributes": True}

class AttachmentOut(BaseModel):
    id: str; item_id: str; name: str; mime: str; size: int; file_path: Optional[str]=None
    model_config = {"from_attributes": True}

class ItemCreate(BaseModel):
    type: str; name: Optional[str]=None; status: Optional[str]="none"
    total: Optional[int]=None; done: Optional[int]=None; closed: Optional[bool]=False
    date: Optional[str]=None; closed_date: Optional[str]=None; note: Optional[str]=None; text: Optional[str]=None
class ItemUpdate(BaseModel):
    name: Optional[str]=None; status: Optional[str]=None; total: Optional[int]=None
    done: Optional[int]=None; closed: Optional[bool]=None; closed_date: Optional[str]=None
    note: Optional[str]=None; text: Optional[str]=None
class ItemOut(BaseModel):
    id: str; section_id: str; type: str; position: int
    name: Optional[str]=None; status: Optional[str]=None; total: Optional[int]=None
    done: Optional[int]=None; closed: Optional[bool]=None; date: Optional[str]=None
    closed_date: Optional[str]=None; note: Optional[str]=None; text: Optional[str]=None
    attachments: list[AttachmentOut] = []
    model_config = {"from_attributes": True}

class SectionCreate(BaseModel):
    title: str; idz_enabled: bool=True; control_enabled: bool=True
class SectionUpdate(BaseModel):
    title: Optional[str]=None; is_open: Optional[bool]=None; idz_enabled: Optional[bool]=None
    control_enabled: Optional[bool]=None; idz: Optional[int]=None; control: Optional[str]=None
class SectionOut(BaseModel):
    id: str; student_id: str; title: str; position: int; is_open: bool
    idz_enabled: bool; control_enabled: bool; idz: int; control: str
    items: list[ItemOut] = []
    model_config = {"from_attributes": True}

class StudentCreate(BaseModel):
    name: str; grade: str="9"; goal: str="ege"; base_rate: int=1500; format: str="online"
    subject_id: Optional[str]=None
class StudentUpdate(BaseModel):
    name: Optional[str]=None; grade: Optional[str]=None; goal: Optional[str]=None
    base_rate: Optional[int]=None; format: Optional[str]=None; subject_id: Optional[str]=None
class StudentOut(BaseModel):
    id: str; name: str; grade: str; goal: str; base_rate: int; format: str
    subject_id: Optional[str]=None; created_by: Optional[str]=None; created_at: Optional[datetime]=None
    sections: list[SectionOut] = []
    model_config = {"from_attributes": True}
class StudentListItem(BaseModel):
    id: str; name: str; grade: str; goal: str; base_rate: int; format: str
    subject_id: Optional[str]=None; created_by: Optional[str]=None; created_at: Optional[datetime]=None
    model_config = {"from_attributes": True}

class BoardOut(BaseModel):
    id: str; student_id: str; strokes: str
    created_at: Optional[datetime]=None; updated_at: Optional[datetime]=None
    model_config = {"from_attributes": True}
