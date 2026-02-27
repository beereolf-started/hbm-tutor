# ╔══════════════════════════════════════════════════════════════╗
# ║  HBM РЕПЕТИТОР — auth.py                                    ║
# ║  Авторизация: bcrypt + JWT                                   ║
# ╚══════════════════════════════════════════════════════════════╝

import bcrypt
import jwt
from datetime import datetime, timedelta, timezone
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session
from database import get_db
from models import User

# Секрет для JWT — на проде вынести в переменную окружения
SECRET_KEY = "hbm-secret-change-me-in-production"
ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = 72


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def create_token(user_id: str, role: str) -> str:
    payload = {
        "sub": user_id,
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXPIRE_HOURS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Токен истёк")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Невалидный токен")


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Извлекает текущего пользователя из заголовка Authorization: Bearer <token>."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Требуется авторизация")
    payload = decode_token(auth[7:])
    user = db.query(User).filter(User.id == payload["sub"]).first()
    if not user:
        raise HTTPException(401, "Пользователь не найден")
    return user


def require_tutor(user: User = Depends(get_current_user)) -> User:
    """Только репетитор."""
    if user.role != "tutor":
        raise HTTPException(403, "Доступ только для репетитора")
    return user
