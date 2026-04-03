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
    subject_ids: list[str]=[]
class UserOut(BaseModel):
    id: str; login: str; role: str; name: str; must_change_password: bool
    student_id: Optional[str]=None; subject_id: Optional[str]=None; created_at: Optional[datetime]=None
    last_seen: Optional[datetime]=None; subject_ids: list[str]=[]
    about: Optional[str]=None; photo: Optional[str]=None
    teamlead_id: Optional[str]=None
    model_config = {"from_attributes": True}

class ProfileUpdate(BaseModel):
    name: Optional[str]=None
    about: Optional[str]=None
    photo: Optional[str]=None

class OwnerProfileUpdate(BaseModel):
    name: Optional[str]=None
    about: Optional[str]=None
    photo: Optional[str]=None
    owner_notes: Optional[str]=None
    login: Optional[str]=None
    password: Optional[str]=None

class ChangeRequestCreate(BaseModel):
    req_type: str
    new_value: str

class NotificationOut(BaseModel):
    id: str; text: str; is_read: bool
    created_at: Optional[datetime]=None
    link: Optional[str]=None; notif_type: Optional[str]=None
    model_config = {"from_attributes": True}

class ContactOut(BaseModel):
    id: str; name: str; role: str; last_seen: Optional[datetime]=None
    model_config = {"from_attributes": True}

class SubjectCreate(BaseModel):
    name: str; icon: str = "📐"
class SubjectUpdate(BaseModel):
    name: Optional[str]=None; icon: Optional[str]=None
class SubjectOut(BaseModel):
    id: str; name: str; icon: str; created_at: Optional[datetime]=None
    model_config = {"from_attributes": True}

class SubblockCreate(BaseModel):
    content: Optional[str]=None

class CourseItemSubblockOut(BaseModel):
    id: str; item_id: str; type: str; content: Optional[str]=None
    name: Optional[str]=None; position: int
    file_path: Optional[str]=None; mime: Optional[str]=None; size: Optional[int]=None
    model_config = {"from_attributes": True}

class ItemSubblockOut(BaseModel):
    id: str; item_id: str; type: str; content: Optional[str]=None
    name: Optional[str]=None; position: int
    file_path: Optional[str]=None; mime: Optional[str]=None; size: Optional[int]=None
    model_config = {"from_attributes": True}

class CourseSectionItemCreate(BaseModel):
    name: str=""; type: str="topic"
    total: Optional[int]=None; text: Optional[str]=None
class CourseSectionItemUpdate(BaseModel):
    name: Optional[str]=None; type: Optional[str]=None
    total: Optional[int]=None; text: Optional[str]=None; note: Optional[str]=None; lang: Optional[str]=None
class CourseSectionItemOut(BaseModel):
    id: str; section_id: str; type: str; position: int; name: str
    total: Optional[int]=None; text: Optional[str]=None; note: Optional[str]=None; lang: Optional[str]=None
    file_path: Optional[str]=None; mime: Optional[str]=None; size: Optional[int]=None
    subblocks: list[CourseItemSubblockOut] = []
    model_config = {"from_attributes": True}
class CourseSectionCreate(BaseModel):
    title: str; idz_enabled: bool=True; control_enabled: bool=True
    idz_text: Optional[str]=None
    items: list[CourseSectionItemCreate] = []
class CourseSectionUpdate(BaseModel):
    title: Optional[str]=None; idz_enabled: Optional[bool]=None; control_enabled: Optional[bool]=None
    idz_text: Optional[str]=None
class CourseSectionOut(BaseModel):
    id: str; course_id: str; title: str; position: int; idz_enabled: bool; control_enabled: bool
    idz_text: Optional[str]=None
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

class SaveAsCourseRequest(BaseModel):
    subject_id: str; title: str; access: str="public"; replace_id: Optional[str]=None

class AttachmentOut(BaseModel):
    id: str; item_id: str; name: str; mime: str; size: int; file_path: Optional[str]=None
    model_config = {"from_attributes": True}

class ItemCreate(BaseModel):
    type: str; name: Optional[str]=None; status: Optional[str]="none"
    total: Optional[int]=None; done: Optional[int]=None; closed: Optional[bool]=False
    date: Optional[str]=None; closed_date: Optional[str]=None; note: Optional[str]=None; text: Optional[str]=None
    grade: Optional[int]=None
class ItemUpdate(BaseModel):
    name: Optional[str]=None; status: Optional[str]=None; total: Optional[int]=None
    done: Optional[int]=None; closed: Optional[bool]=None; closed_date: Optional[str]=None
    note: Optional[str]=None; text: Optional[str]=None; grade: Optional[int]=None
    student_answer: Optional[str]=None; lang: Optional[str]=None
class ItemOut(BaseModel):
    id: str; section_id: str; type: str; position: int
    name: Optional[str]=None; status: Optional[str]=None; total: Optional[int]=None
    done: Optional[int]=None; closed: Optional[bool]=None; date: Optional[str]=None
    closed_date: Optional[str]=None; note: Optional[str]=None; text: Optional[str]=None
    grade: Optional[int]=None; student_answer: Optional[str]=None; lang: Optional[str]=None
    attachments: list[AttachmentOut] = []
    subblocks: list[ItemSubblockOut] = []
    model_config = {"from_attributes": True}

class SectionCreate(BaseModel):
    title: str; idz_enabled: bool=True; control_enabled: bool=True; idz_text: Optional[str]=None
    course_id: Optional[str]=None
class SectionUpdate(BaseModel):
    title: Optional[str]=None; is_open: Optional[bool]=None; idz_enabled: Optional[bool]=None
    control_enabled: Optional[bool]=None; idz: Optional[int]=None; control: Optional[str]=None
    locked: Optional[bool]=None; idz_text: Optional[str]=None; course_id: Optional[str]=None
class SectionOut(BaseModel):
    id: str; student_id: str; title: str; position: int; is_open: bool
    idz_enabled: bool; control_enabled: bool; idz: int; control: str
    locked: bool=False; idz_text: Optional[str]=None; course_id: Optional[str]=None
    items: list[ItemOut] = []
    model_config = {"from_attributes": True}

class StudentCourseCreate(BaseModel):
    title: str; tutor_id: Optional[str]=None
class StudentCourseUpdate(BaseModel):
    title: Optional[str]=None; tutor_id: Optional[str]=None
class StudentCourseOut(BaseModel):
    id: str; student_id: str; title: str
    tutor_id: Optional[str]=None; tutor_name: Optional[str]=None
    sections: list[SectionOut] = []
    model_config = {"from_attributes": True}

class StudentCreate(BaseModel):
    name: str; level: str="school"; grade: Optional[str]=None; goal: str="ege"
    base_rate: int=1500; format: str="online"
    subject_id: Optional[str]=None
class StudentUpdate(BaseModel):
    name: Optional[str]=None; level: Optional[str]=None; grade: Optional[str]=None
    goal: Optional[str]=None; base_rate: Optional[int]=None; format: Optional[str]=None
    subject_id: Optional[str]=None; rewards_enabled: Optional[bool]=None
class StudentOut(BaseModel):
    id: str; name: str; level: str="school"; grade: Optional[str]=None; goal: str; base_rate: int; format: str
    rewards_enabled: bool=True
    subject_id: Optional[str]=None; created_by: Optional[str]=None; created_at: Optional[datetime]=None
    sections: list[SectionOut] = []
    courses: list[StudentCourseOut] = []
    model_config = {"from_attributes": True}
class StudentListItem(BaseModel):
    id: str; name: str; level: str="school"; grade: Optional[str]=None; goal: str; base_rate: int; format: str
    rewards_enabled: bool=True
    subject_id: Optional[str]=None; created_by: Optional[str]=None; created_at: Optional[datetime]=None
    model_config = {"from_attributes": True}

class BoardOut(BaseModel):
    id: str; student_id: str; strokes: str
    created_at: Optional[datetime]=None; updated_at: Optional[datetime]=None
    model_config = {"from_attributes": True}

from datetime import datetime as dt_type
class MessageCreate(BaseModel):
    to_id: str; text: str
class MessageOut(BaseModel):
    id: str; from_id: str; to_id: str; text: str; is_read: bool
    created_at: Optional[dt_type]=None; from_name: str=""; to_name: str=""
    model_config = {"from_attributes": True}

class ScheduleSlotCreate(BaseModel):
    student_id: Optional[str]=None
    day_of_week: int
    slot_index: int
    duration: int=2
    note: Optional[str]=None
    color: Optional[str]=None

class PersonalBoardCreate(BaseModel):
    title: str="Новая доска"
class PersonalBoardUpdate(BaseModel):
    title: Optional[str]=None
class PersonalBoardOut(BaseModel):
    id: str; owner_id: str; title: str; strokes: str
    share_token: Optional[str]=None
    created_at: Optional[dt_type]=None; updated_at: Optional[dt_type]=None
    model_config = {"from_attributes": True}
class PersonalBoardListItem(BaseModel):
    id: str; owner_id: str; title: str; share_token: Optional[str]=None
    created_at: Optional[dt_type]=None; updated_at: Optional[dt_type]=None
    is_owner: bool = True
    owner_name: Optional[str] = None
    member_count: int = 0
    model_config = {"from_attributes": True}

class BoardMemberOut(BaseModel):
    id: str; name: str; role: str
    model_config = {"from_attributes": True}

class BoardInviteOut(BaseModel):
    id: str; board_id: str; board_title: str; from_id: str; from_name: str
    created_at: Optional[dt_type] = None
    model_config = {"from_attributes": True}
