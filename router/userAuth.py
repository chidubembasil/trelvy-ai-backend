from fastapi import APIRouter, HTTPException, BackgroundTasks, Depends
from starlette import status
from database import db
from schema import User, UserUpdate, LoginDetails, VerifyOTPDetails
from runtime.auth import hash_password, verify_password, create_access_token, get_current_user
from runtime.email import generate_otp, send_otp_email
from datetime import datetime, timedelta

router = APIRouter(
    prefix="/auth",
    tags=["Auth"],
)

# ── Register ──────────────────────────────────────────────
@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(details: User):
    existing = await db.user.find_unique(where={"email": details.email})
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered"
        )
    await db.user.create(
        data={
            "name":     details.name,
            "email":    details.email,
            "password": hash_password(details.password),
        }
    )
    return {"detail": "User created successfully"}

# ── Login → sends OTP in background ──────────────────────
@router.post("/login", status_code=status.HTTP_200_OK)
async def login(details: LoginDetails, background_tasks: BackgroundTasks):
    user = await db.user.find_unique(where={"email": details.email})
    if not user or not verify_password(details.password, user.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password"
        )

    otp = generate_otp()
    otp_expiry = datetime.utcnow() + timedelta(minutes=10)

    await db.user.update(
        where={"id": user.id},
        data={
            "otp":       otp,
            "otpExpiry": otp_expiry,
        }
    )

    background_tasks.add_task(send_otp_email, user.email, otp, user.name)

    return {"detail": "OTP sent to your email"}

# ── Verify OTP → returns token + creates session ──────────
@router.post("/verify-otp", status_code=status.HTTP_200_OK)
async def verify_otp(details: VerifyOTPDetails):
    user = await db.user.find_unique(where={"email": details.email})
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    if user.otp != details.otp:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid OTP"
        )

    if datetime.utcnow() > user.otpExpiry:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OTP has expired — please login again"
        )

    # Clear OTP
    await db.user.update(
        where={"id": user.id},
        data={
            "otp":        None,
            "otpExpiry":  None,
            "isVerified": True,
        }
    )

    # Create token
    token = create_access_token(user.id)

    # Create session in database
    await db.session.create(
        data={
            "token":     token,
            "userId":    user.id,
            "expiresAt": datetime.utcnow() + timedelta(hours=24),
        }
    )

    return {
        "access_token": token,
        "token_type":   "bearer",
        "detail":       "Login successful"
    }

# ── Get current user ──────────────────────────────────────
@router.get("/me", status_code=status.HTTP_200_OK)
async def get_me(current_user=Depends(get_current_user)):
    return {
        "id":    current_user.id,
        "name":  current_user.name,
        "email": current_user.email,
    }

# ── Get all active sessions for current user ──────────────
@router.get("/sessions", status_code=status.HTTP_200_OK)
async def get_sessions(current_user=Depends(get_current_user)):
    sessions = await db.session.find_many(
        where={
            "userId":    current_user.id,
            "expiresAt": {"gt": datetime.utcnow()}  # only active sessions
        }
    )
    return {
        "total":    len(sessions),
        "sessions": [
            {
                "id":        s.id,
                "createdAt": s.createdAt,
                "expiresAt": s.expiresAt,
            }
            for s in sessions
        ]
    }

# ── Logout → deletes current session ─────────────────────
@router.post("/logout", status_code=status.HTTP_200_OK)
async def logout(current_user=Depends(get_current_user), token: str = Depends(get_current_user)):
    # Delete this specific session from database
    await db.session.delete_many(
        where={
            "userId": current_user.id,
            "token":  token if isinstance(token, str) else token.token
        }
    )
    return {"detail": "Logged out successfully"}

# ── Logout all → deletes ALL sessions for user ────────────
@router.post("/logout-all", status_code=status.HTTP_200_OK)
async def logout_all(current_user=Depends(get_current_user)):
    deleted = await db.session.delete_many(
        where={"userId": current_user.id}
    )
    return {"detail": f"Logged out of all {deleted} sessions"}

# ── Update user ───────────────────────────────────────────
@router.patch("/update", status_code=status.HTTP_200_OK)
async def update_user(
    details: UserUpdate,
    current_user=Depends(get_current_user)
):
    update_data = {}
    if details.name:
        update_data["name"] = details.name
    if details.email:
        existing = await db.user.find_unique(where={"email": details.email})
        if existing and existing.id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already taken"
            )
        update_data["email"] = details.email
    if details.password:
        update_data["password"] = hash_password(details.password)

    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields to update"
        )

    updated_user = await db.user.update(
        where={"id": current_user.id},
        data=update_data
    )
    return {
        "detail": "User updated successfully",
        "user": {
            "id":    updated_user.id,
            "name":  updated_user.name,
            "email": updated_user.email,
        }
    }

# ── Delete user ───────────────────────────────────────────
@router.delete("/delete", status_code=status.HTTP_200_OK)
async def delete_user(current_user=Depends(get_current_user)):
    # Delete sessions first
    await db.session.delete_many(
        where={"userId": current_user.id}
    )
    # Delete all agents
    await db.multiAgent.delete_many(
        where={"userId": current_user.id}
    )
    # Delete user
    await db.user.delete(
        where={"id": current_user.id}
    )
    return {"detail": "User and all their data deleted successfully"}