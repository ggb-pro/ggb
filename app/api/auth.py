from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from jose import jwt, JWTError

from app.deps import get_db
from app.config import get_settings
from app.models.user import User
from app.schemas.user import UserCreate, UserLogin, UserOut, UserUpdate, PasswordChange, RefreshRequest, Token
from app.utils.security import (
    hash_password, verify_password, create_access_token, create_refresh_token, get_current_user,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
settings = get_settings()


@router.post("/register", response_model=UserOut)
async def register(data: UserCreate, db: AsyncSession = Depends(get_db)):
    exists = await db.execute(select(User).where(User.email == data.email))
    if exists.scalar_one_or_none():
        raise HTTPException(400, "Email already registered")
    user = User(email=data.email, password_hash=hash_password(data.password), display_name=data.display_name)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/login", response_model=Token)
async def login(data: UserLogin, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == data.email))
    user = result.scalar_one_or_none()
    if not user or not verify_password(data.password, user.password_hash):
        raise HTTPException(401, "Invalid email or password")
    return Token(
        access_token=create_access_token(str(user.id)),
        refresh_token=create_refresh_token(str(user.id)),
    )


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)):
    return user


@router.patch("/me", response_model=UserOut)
async def update_me(
    data: UserUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    update_data = data.model_dump(exclude_unset=True)
    for k, v in update_data.items():
        setattr(user, k, v)
    await db.commit()
    await db.refresh(user)
    return user


@router.put("/password")
async def change_password(
    data: PasswordChange,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not verify_password(data.old_password, user.password_hash):
        raise HTTPException(400, "Old password is incorrect")
    user.password_hash = hash_password(data.new_password)
    await db.commit()
    return {"status": "updated"}


@router.post("/refresh", response_model=Token)
async def refresh_token(data: RefreshRequest, db: AsyncSession = Depends(get_db)):
    try:
        payload = jwt.decode(data.refresh_token, settings.secret_key, algorithms=["HS256"])
        if payload.get("type") != "refresh":
            raise HTTPException(401, "Invalid refresh token")
        user_id = payload.get("sub")
    except JWTError:
        raise HTTPException(401, "Invalid or expired refresh token")

    result = await db.execute(select(User).where(User.id == user_id, User.status == "active"))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(401, "User not found")

    return Token(
        access_token=create_access_token(str(user.id)),
        refresh_token=create_refresh_token(str(user.id)),
    )
