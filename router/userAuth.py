from fastapi import APIRouter, HTTPException, Depends
from starlette import status
from database import db
from schema import UserRegister, UserUpdate, LoginDetails
from runtime.auth import hash_password, verify_password, create_access_token, get_current_user
from datetime import datetime, timedelta

router = APIRouter(
    prefix="/auth",
    tags=["Auth"],
)

@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(details: UserRegister):
    existing = await db.user.find_unique(where={"email": details.email})
    if existing:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email already registered")
    new_user = await db.user.create(
        data={"name": details.name, "email": details.email, "password": hash_password(details.password), "isVerified": True}
    )
    token = create_access_token(new_user.id)
    await db.session.create(data={"token": token, "userId": new_user.id, "expiresAt": datetime.utcnow() + timedelta(hours=24)})
    return {"access_token": token, "token_type": "bearer", "detail": "Registered successfully"}

@router.post("/login", status_code=status.HTTP_200_OK)
async def login(details: LoginDetails):
    user = await db.user.find_unique(where={"email": details.email})
    if not user or not verify_password(details.password, user.password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    token = create_access_token(user.id)
    await db.session.create(data={"token": token, "userId": user.id, "expiresAt": datetime.utcnow() + timedelta(hours=24)})
    return {"access_token": token, "token_type": "bearer", "detail": "Login successful"}

@router.get("/me", status_code=status.HTTP_200_OK)
async def get_me(current_user=Depends(get_current_user)):
    return {"id": current_user.id, "name": current_user.name, "email": current_user.email}

@router.post("/logout", status_code=status.HTTP_200_OK)
async def logout(current_user=Depends(get_current_user)):
    await db.session.delete_many(where={"userId": current_user.id})
    return {"detail": "Logged out successfully"}

@router.patch("/update", status_code=status.HTTP_200_OK)
async def update_user(details: UserUpdate, current_user=Depends(get_current_user)):
    update_data = {}
    if details.name:
        update_data["name"] = details.name
    if details.email:
        existing = await db.user.find_unique(where={"email": details.email})
        if existing and existing.id != current_user.id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email already taken")
        update_data["email"] = details.email
    if details.password:
        update_data["password"] = hash_password(details.password)
    if not update_data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields to update")
    updated_user = await db.user.update(where={"id": current_user.id}, data=update_data)
    return {"detail": "User updated successfully", "user": {"id": updated_user.id, "name": updated_user.name, "email": updated_user.email}}

@router.delete("/delete", status_code=status.HTTP_200_OK)
async def delete_user(current_user=Depends(get_current_user)):
    await db.session.delete_many(where={"userId": current_user.id})
    await db.multiAgent.delete_many(where={"userId": current_user.id})
    await db.user.delete(where={"id": current_user.id})
    return {"detail": "User and all their data deleted successfully"}