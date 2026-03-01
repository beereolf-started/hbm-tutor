import os, bcrypt, jwt
from datetime import datetime, timedelta, timezone
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session
from database import get_db
from models import User

SECRET_KEY = os.environ.get("HBM_JWT_SECRET", "hbm-secret-change-me-in-production")
ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = 72

def hash_password(pw): return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
def verify_password(pw, h): return bcrypt.checkpw(pw.encode(), h.encode())
def create_token(uid, role):
    return jwt.encode({"sub":uid,"role":role,"exp":datetime.now(timezone.utc)+timedelta(hours=TOKEN_EXPIRE_HOURS)}, SECRET_KEY, algorithm=ALGORITHM)
def decode_token(token):
    try: return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError: raise HTTPException(401, "Токен истёк")
    except jwt.InvalidTokenError: raise HTTPException(401, "Невалидный токен")
def get_current_user(request: Request, db: Session = Depends(get_db)):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "): raise HTTPException(401, "Требуется авторизация")
    u = db.query(User).filter(User.id == decode_token(auth[7:])["sub"]).first()
    if not u: raise HTTPException(401, "Пользователь не найден")
    return u
def require_owner(user: User = Depends(get_current_user)):
    if user.role != "owner": raise HTTPException(403, "Только для владельца")
    return user
def require_tutor_or_owner(user: User = Depends(get_current_user)):
    if user.role not in ("owner", "tutor"): raise HTTPException(403, "Только для преподавателей")
    return user
